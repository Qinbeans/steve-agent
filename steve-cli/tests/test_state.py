"""previous_response_id: continuation, forks, store false, deletion, eviction, and what must not carry over."""

import threading

import httpx
import openai
import pytest
from fake_upstream import Step
from test_server import assert_error, raw_post, sse_events, wait_until


def checkpointed_threads(server):
    return set(server.agent.checkpointer.storage)


def user_texts(body):
    texts = []
    for item in body["input"]:
        if item.get("role") == "user":
            content = item["content"]
            texts.append(content if isinstance(content, str) else "".join(part["text"] for part in content))
    return texts


def test_continuation_sends_history_and_echoes_the_parent(server, upstream):
    first = server.client.responses.create(input="my name is ryan")
    second = server.client.responses.create(input="what is my name", previous_response_id=first.id)

    assert second.previous_response_id == first.id
    assert first.previous_response_id is None
    assert second.id != first.id
    assert user_texts(upstream.requests[1]["body"]) == ["my name is ryan", "what is my name"]
    assert "echo: my name is ryan" in upstream.body_text(1)


def test_continuation_keeps_tool_history_across_turns(server, upstream):
    upstream.steps.extend([Step(text="Looking.", tools=[("ls", '{"path": "/"}')]), Step(text="Listed.")])
    first = server.client.responses.create(input="list")
    server.client.responses.create(input="again", previous_response_id=first.id)

    third_call = upstream.body_text(2)
    assert '"function_call_output"' in third_call
    assert "Listed." in third_call


def test_streamed_response_can_be_continued_as_soon_as_it_ends(server, upstream):
    with server.client.responses.stream(input="one", model="m") as stream:
        first = stream.get_final_response()

    second = server.client.responses.create(input="two", previous_response_id=first.id)

    assert user_texts(upstream.requests[1]["body"]) == ["one", "two"]
    assert second.output_text == "echo: two"


def test_forks_are_isolated(server, upstream):
    root = server.client.responses.create(input="root")
    left = server.client.responses.create(input="left", previous_response_id=root.id)
    right = server.client.responses.create(input="right", previous_response_id=root.id)
    left_two = server.client.responses.create(input="left two", previous_response_id=left.id)
    right_two = server.client.responses.create(input="right two", previous_response_id=right.id)

    assert user_texts(upstream.requests[2]["body"]) == ["root", "right"]
    assert user_texts(upstream.requests[3]["body"]) == ["root", "left", "left two"]
    assert user_texts(upstream.requests[4]["body"]) == ["root", "right", "right two"]
    assert left_two.output_text == "echo: left two"
    assert right_two.output_text == "echo: right two"


def test_two_concurrent_forks_are_isolated(server, upstream):
    root = server.client.responses.create(input="root")
    barrier = threading.Barrier(2)
    upstream.steps.extend([Step(barrier=barrier), Step(barrier=barrier)])
    results = {}

    def fork(tag):
        results[tag] = server.client.responses.create(input=tag, previous_response_id=root.id)

    threads = [threading.Thread(target=fork, args=(tag,)) for tag in ("left", "right")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results["left"].output_text == "echo: left"
    assert results["right"].output_text == "echo: right"
    assert sorted(user_texts(request["body"])[-1] for request in upstream.requests[1:]) == ["left", "right"]
    for request in upstream.requests[1:]:
        assert user_texts(request["body"])[0] == "root" and len(user_texts(request["body"])) == 2

    left_next = server.client.responses.create(input="next", previous_response_id=results["left"].id)
    assert user_texts(upstream.requests[-1]["body"]) == ["root", "left", "next"]
    right_next = server.client.responses.create(input="next", previous_response_id=results["right"].id)
    assert user_texts(upstream.requests[-1]["body"]) == ["root", "right", "next"]
    assert left_next.id != right_next.id


def test_store_false_without_parent_leaves_nothing(server):
    response = server.client.responses.create(input="x", store=False)

    assert response.store is False
    assert response.output_text == "echo: x"
    assert_error(httpx.get(f"{server.url}/v1/responses/{response.id}"), 404, "response_not_found")
    assert_error(raw_post(server, {"input": "y", "previous_response_id": response.id}), 404, "previous_response_not_found")
    wait_until(lambda: checkpointed_threads(server) == set())


def test_store_false_with_parent_records_nothing_but_keeps_the_thread(server, upstream):
    root = server.client.responses.create(input="root")
    child = server.client.responses.create(input="child", store=False, previous_response_id=root.id)

    assert user_texts(upstream.requests[1]["body"]) == ["root", "child"]
    assert_error(httpx.get(f"{server.url}/v1/responses/{child.id}"), 404)
    assert len(checkpointed_threads(server)) == 1

    # The stored parent still continues without the unstored child.
    server.client.responses.create(input="other", previous_response_id=root.id)
    assert user_texts(upstream.requests[2]["body"]) == ["root", "other"]


def test_get_returns_the_stored_response(server):
    created = server.client.responses.create(input="x", metadata={"a": "b"})

    fetched = server.client.responses.retrieve(created.id)

    assert fetched.model_dump() == created.model_dump()


def test_delete_removes_the_record_only(server, upstream):
    root = server.client.responses.create(input="root")
    child = server.client.responses.create(input="child", previous_response_id=root.id)

    deleted = httpx.delete(f"{server.url}/v1/responses/{root.id}")

    assert deleted.status_code == 200
    assert deleted.json() == {"id": root.id, "object": "response", "deleted": True}
    assert_error(httpx.get(f"{server.url}/v1/responses/{root.id}"), 404)
    assert_error(httpx.delete(f"{server.url}/v1/responses/{root.id}"), 404, "response_not_found")
    assert_error(raw_post(server, {"input": "x", "previous_response_id": root.id}), 404, "previous_response_not_found")

    # The child's history is in its own checkpoints, so it survives the parent's deletion.
    server.client.responses.create(input="more", previous_response_id=child.id)
    assert user_texts(upstream.requests[-1]["body"]) == ["root", "child", "more"]


def test_deleting_the_last_response_deletes_the_thread(server):
    root = server.client.responses.create(input="root")
    child = server.client.responses.create(input="child", previous_response_id=root.id)
    assert len(checkpointed_threads(server)) == 1

    server.client.responses.delete(root.id)
    assert len(checkpointed_threads(server)) == 1
    server.client.responses.delete(child.id)
    assert checkpointed_threads(server) == set()


def test_delete_during_a_child_run_keeps_the_thread_until_it_ends(server, upstream):
    root = server.client.responses.create(input="root")
    upstream.steps.append(Step(text="hello world", hold=True))
    done = []
    thread = threading.Thread(
        target=lambda: done.append(server.client.responses.create(input="child", previous_response_id=root.id))
    )
    thread.start()
    assert upstream.holding.wait(10)

    server.client.responses.delete(root.id)
    assert len(checkpointed_threads(server)) == 1

    upstream.release.set()
    thread.join()
    assert done[0].output_text == "hello world"
    server.client.responses.create(input="more", previous_response_id=done[0].id)
    assert user_texts(upstream.requests[-1]["body"]) == ["root", "child", "more"]


def test_eviction_removes_the_least_recently_used_conversation(start_server):
    server = start_server(max_conversations=2)

    first = server.client.responses.create(input="first")
    first_child = server.client.responses.create(input="child", previous_response_id=first.id)
    second = server.client.responses.create(input="second")
    server.client.responses.create(input="touch", previous_response_id=first_child.id)
    third = server.client.responses.create(input="third")

    # second was used least recently, because first's thread was continued after it.
    assert_error(httpx.get(f"{server.url}/v1/responses/{second.id}"), 404)
    assert_error(raw_post(server, {"input": "x", "previous_response_id": second.id}), 404, "previous_response_not_found")
    server.client.responses.retrieve(first.id)
    server.client.responses.retrieve(first_child.id)
    server.client.responses.retrieve(third.id)
    assert len(checkpointed_threads(server)) == 2


def test_eviction_removes_every_response_of_the_thread(start_server):
    server = start_server(max_conversations=1)

    first = server.client.responses.create(input="first")
    child = server.client.responses.create(input="child", previous_response_id=first.id)
    server.client.responses.create(input="other")

    assert_error(httpx.get(f"{server.url}/v1/responses/{first.id}"), 404)
    assert_error(httpx.get(f"{server.url}/v1/responses/{child.id}"), 404)
    assert len(checkpointed_threads(server)) == 1


def test_a_thread_with_a_run_in_flight_is_not_evicted(start_server, upstream):
    server = start_server(max_conversations=1)
    parent = server.client.responses.create(input="parent")
    upstream.steps.append(Step(text="hello world", hold=True))
    done = []
    thread = threading.Thread(
        target=lambda: done.append(server.client.responses.create(input="child", previous_response_id=parent.id))
    )
    thread.start()
    assert upstream.holding.wait(10)

    other = server.client.responses.create(input="other")
    server.client.responses.retrieve(other.id)
    another = server.client.responses.create(input="another")

    server.client.responses.retrieve(parent.id)
    assert_error(httpx.get(f"{server.url}/v1/responses/{other.id}"), 404)
    server.client.responses.retrieve(another.id)

    upstream.release.set()
    thread.join()
    child = done[0]
    assert child.output_text == "hello world"
    assert_error(httpx.get(f"{server.url}/v1/responses/{another.id}"), 404)
    assert len(checkpointed_threads(server)) == 1

    server.client.responses.create(input="more", previous_response_id=child.id)
    assert user_texts(upstream.requests[-1]["body"]) == ["parent", "child", "more"]


def test_unknown_previous_response_is_404(server, upstream):
    with pytest.raises(openai.NotFoundError) as caught:
        server.client.responses.create(input="x", previous_response_id="resp_nope")

    assert caught.value.code == "previous_response_not_found"
    assert caught.value.param == "previous_response_id"
    assert upstream.requests == []
    assert not server.app.state.runs


def test_rejected_input_does_not_pin_a_thread(start_server):
    server = start_server(max_conversations=1)
    root = server.client.responses.create(input="root")

    assert_error(raw_post(server, {"input": [{"type": "item_reference"}], "previous_response_id": root.id}), 400)

    server.client.responses.create(input="other")
    assert_error(httpx.get(f"{server.url}/v1/responses/{root.id}"), 404)


def test_memory_edit_is_visible_on_the_next_turn(server, upstream, steve_home):
    first = server.client.responses.create(input="one")
    assert "HOME_AGENTS_V1" in upstream.body_text(0)
    assert "MEMORY_V1" in upstream.body_text(0)

    (steve_home / "agents" / "AGENTS.md").write_text("HOME_AGENTS_V2")
    (steve_home / "agents" / "memories" / "MEMORY.md").write_text("MEMORY_V2")
    server.client.responses.create(input="two", previous_response_id=first.id)

    assert "HOME_AGENTS_V2" in upstream.body_text(1)
    assert "MEMORY_V2" in upstream.body_text(1)
    assert "HOME_AGENTS_V1" not in upstream.body_text(1)
    assert "MEMORY_V1" not in upstream.body_text(1)


def test_new_skill_is_visible_on_the_next_turn(server, upstream, steve_home):
    first = server.client.responses.create(input="one")
    skill = steve_home / "agents" / "skills" / "fresh"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: fresh\ndescription: NEW_SKILL_MARK\n---\nbody\n")

    server.client.responses.create(input="two", previous_response_id=first.id)

    assert "NEW_SKILL_MARK" not in upstream.body_text(0)
    assert "NEW_SKILL_MARK" in upstream.body_text(1)


def test_dirty_cancelled_run_does_not_poison_a_continuation(server, upstream):
    root = server.client.responses.create(input="root")
    upstream.steps.append(Step(text="doomed reply", hold=True))
    payload = {"input": "doomed", "stream": True, "previous_response_id": root.id}

    with httpx.stream("POST", f"{server.url}/v1/responses", json=payload) as response:
        response_id = None
        for line in response.iter_lines():
            if line.startswith("data:") and response_id is None:
                response_id = sse_events(f"event: response.created\n{line}\n\n")[0]["response"]["id"]
            if line.startswith("event: response.output_text.delta"):
                break

    assert upstream.hung_up.wait(10)
    wait_until(lambda: not server.app.state.runs)
    assert_error(httpx.get(f"{server.url}/v1/responses/{response_id}"), 404)
    assert_error(raw_post(server, {"input": "x", "previous_response_id": response_id}), 404)

    server.client.responses.create(input="clean", previous_response_id=root.id)
    assert user_texts(upstream.requests[-1]["body"]) == ["root", "clean"]
    assert "doomed" not in upstream.body_text(-1)


def test_cancelled_first_turn_deletes_its_thread(server, upstream):
    upstream.steps.append(Step(text="doomed reply", hold=True))

    with httpx.stream("POST", f"{server.url}/v1/responses", json={"input": "x", "stream": True}) as response:
        for line in response.iter_lines():
            if line.startswith("event: response.output_text.delta"):
                break

    assert upstream.hung_up.wait(10)
    wait_until(lambda: not server.app.state.runs)
    wait_until(lambda: checkpointed_threads(server) == set())


def test_instructions_are_not_carried_over(server, upstream):
    first = server.client.responses.create(
        input=[{"role": "system", "content": "SYSTEM_ONE"}, {"role": "user", "content": "one"}],
        instructions="INSTRUCTIONS_ONE",
    )
    second = server.client.responses.create(input="two", previous_response_id=first.id, instructions="INSTRUCTIONS_TWO")
    server.client.responses.create(input="three", previous_response_id=second.id)

    assert "INSTRUCTIONS_ONE" in upstream.body_text(0)
    assert "SYSTEM_ONE" in upstream.body_text(0)
    assert "INSTRUCTIONS_ONE" not in upstream.body_text(1)
    assert "SYSTEM_ONE" not in upstream.body_text(1)
    assert "INSTRUCTIONS_TWO" in upstream.body_text(1)
    assert "INSTRUCTIONS_TWO" not in upstream.body_text(2)
    assert second.instructions == "INSTRUCTIONS_TWO"


def test_failed_response_is_retrievable_and_continues_from_its_head(server, upstream):
    upstream.steps.append(Step(http_error=(400, "boom")))
    response = raw_post(server, {"input": "fails", "stream": False})
    assert_error(response, 500)
    records = server.app.state.store._records
    (failed_id, record), = records.items()
    assert record.response.status == "failed"
    assert record.checkpoint_id

    assert server.client.responses.retrieve(failed_id).status == "failed"
    server.client.responses.create(input="again", previous_response_id=failed_id)
    assert user_texts(upstream.requests[-1]["body"])[-1] == "again"


def test_failed_response_without_a_head_cannot_be_continued(server):
    store = server.app.state.store
    response = server.client.responses.create(input="x")
    store.get(response.id).checkpoint_id = None

    error = assert_error(raw_post(server, {"input": "y", "previous_response_id": response.id}), 400)

    assert error["code"] == "previous_response_not_continuable"
    assert error["param"] == "previous_response_id"


def test_incomplete_response_can_be_continued(server, upstream):
    upstream.steps.append(Step(text="partial", end="incomplete"))
    first = server.client.responses.create(input="one")
    assert first.status == "incomplete"

    server.client.responses.create(input="two", previous_response_id=first.id)

    assert user_texts(upstream.requests[-1]["body"]) == ["one", "two"]


def test_input_items_are_this_requests_only(server):
    first = server.client.responses.create(input="one")
    second = server.client.responses.create(
        input=[{"role": "user", "content": "two"}, {"role": "user", "content": [{"type": "input_text", "text": "three"}]}],
        previous_response_id=first.id,
    )

    page = server.client.responses.input_items.list(second.id, order="asc")

    assert [item.content[0].text for item in page.data] == ["two", "three"]
    assert all(item.id for item in page.data)
    assert [item.content[0].text for item in server.client.responses.input_items.list(first.id).data] == ["one"]


def test_input_items_paging(server):
    items = [{"role": "user", "content": str(i)} for i in range(5)]
    response = server.client.responses.create(input=items)
    url = f"{server.url}/v1/responses/{response.id}/input_items"

    def texts(page):
        return [item["content"][0]["text"] for item in page["data"]]

    default = httpx.get(url).json()
    assert default["object"] == "list"
    assert texts(default) == ["4", "3", "2", "1", "0"]
    assert default["first_id"] == default["data"][0]["id"]
    assert default["last_id"] == default["data"][-1]["id"]
    assert default["has_more"] is False

    first_page = httpx.get(url, params={"limit": 2, "order": "asc"}).json()
    assert texts(first_page) == ["0", "1"] and first_page["has_more"] is True
    second_page = httpx.get(url, params={"limit": 2, "order": "asc", "after": first_page["last_id"]}).json()
    assert texts(second_page) == ["2", "3"] and second_page["has_more"] is True
    last_page = httpx.get(url, params={"limit": 2, "order": "asc", "after": second_page["last_id"]}).json()
    assert texts(last_page) == ["4"] and last_page["has_more"] is False

    # The SDK follows the cursor by itself.
    assert len(list(server.client.responses.input_items.list(response.id, limit=2))) == 5

    assert_error(httpx.get(url, params={"limit": 0}), 400)
    assert_error(httpx.get(url, params={"order": "sideways"}), 400)
    assert_error(httpx.get(url, params={"after": "nope"}), 400, "invalid_cursor")
    assert_error(httpx.get(f"{server.url}/v1/responses/resp_nope/input_items"), 404)


def test_state_routes_need_the_api_key(start_server):
    server = start_server(api_key="s3cret")
    response = server.client.responses.create(input="x")

    for call in (httpx.get, httpx.delete):
        assert_error(call(f"{server.url}/v1/responses/{response.id}"), 401)
    assert_error(httpx.get(f"{server.url}/v1/responses/{response.id}/input_items"), 401)
    assert server.client.responses.retrieve(response.id).id == response.id
