"""Regressions from the review of the Responses adapter, run through the real SDK, server and agent."""

import json
import os
import re
import stat
from collections import deque
from pathlib import Path

import httpx
import openai
import pytest
from fake_upstream import Step
from test_cli import config_home, run_cli, spy_uvicorn  # noqa: F401 (spy_uvicorn is a fixture)

SECRET = "TOPSECRET-TOKEN"
OVERSIZED = SECRET + "\n" + "\n".join(["word " * 40] * 1100)  # over the 200k chars that trigger eviction
HISTORY_PATH = re.compile(r"/[\w/]*conversation_history/[\w-]+\.md")


def files_holding(home, marker):
    return [path for path in Path(home).rglob("*") if path.is_file() and marker in path.read_text(errors="ignore")]


def read_back(server, upstream, path, **request):
    """Have the model read a file, and return what the tool gave it."""
    upstream.steps.extend([Step(text="", tools=[("read_file", json.dumps({"file_path": path}))]), Step(text="done")])
    response = server.client.responses.create(input="read it", **request)
    item = next(item for item in response.output if item.type == "mcp_call")
    return item.output or str(item.error)


def offload_path(upstream):
    return HISTORY_PATH.search(upstream.body_text()).group(0)


# Offloaded conversation content lives and dies with the conversation


def test_store_false_input_leaves_nothing_on_disk_or_in_other_conversations(start_server, upstream, steve_home):
    server = start_server()

    server.client.responses.create(input=OVERSIZED, store=False)

    assert files_holding(steve_home, SECRET) == []
    assert SECRET not in read_back(server, upstream, offload_path(upstream))


def test_offloaded_input_stays_readable_in_its_own_conversation(server, upstream):
    first = server.client.responses.create(input=OVERSIZED)

    assert SECRET in read_back(server, upstream, offload_path(upstream), previous_response_id=first.id)


def test_delete_removes_offloaded_input(server, upstream, steve_home):
    response = server.client.responses.create(input=OVERSIZED)
    path = offload_path(upstream)

    server.client.responses.delete(response.id)

    assert files_holding(steve_home, SECRET) == []
    assert SECRET not in read_back(server, upstream, path)


def test_eviction_removes_offloaded_input(start_server, upstream, steve_home):
    server = start_server(max_conversations=1)
    evicted = server.client.responses.create(input=OVERSIZED)
    path = offload_path(upstream)

    other = server.client.responses.create(input="next")

    with pytest.raises(openai.NotFoundError):
        server.client.responses.retrieve(evicted.id)
    assert files_holding(steve_home, SECRET) == []
    assert SECRET not in read_back(server, upstream, path, previous_response_id=other.id)


def test_large_tool_results_stay_inside_the_conversation(server, upstream, steve_home):
    upstream.routes["ALPHA"] = deque([Step(text=OVERSIZED)])
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="done")])

    first = server.client.responses.create(input="go")

    path = re.search(r"/artifacts/large_tool_results/\w+", upstream.body_text()).group(0)
    assert files_holding(steve_home, SECRET) == []
    assert SECRET in read_back(server, upstream, path, previous_response_id=first.id)
    assert SECRET not in read_back(server, upstream, path)


def turn(server, upstream, mark, previous=None):
    """One turn with a large input. Extra steps feed any summarization call it triggers."""
    upstream.steps.extend(Step(text="ok") for _ in range(3))
    chunk = "\n".join(["word " * 40] * 950)
    kwargs = {"previous_response_id": previous.id} if previous else {}
    response = server.client.responses.create(input=f"{mark}-MARK\n{chunk}", **kwargs)
    upstream.steps.clear()
    assert response.status == "completed"
    return response


def summarized_conversation(server, upstream):
    head = None
    for index in range(4):
        head = turn(server, upstream, f"ROOT{index}", head)
    return head


def test_summarization_history_is_not_left_on_disk(server, upstream, steve_home):
    summarized_conversation(server, upstream)

    assert "session_" in upstream.body_text()
    assert files_holding(steve_home, "ROOT0-MARK") == []


def test_forks_do_not_see_each_others_history(server, upstream):
    left = right = summarized_conversation(server, upstream)
    for index in range(6):
        left = turn(server, upstream, f"LEFT{index}", left)
        right = turn(server, upstream, f"RIGHT{index}", right)

    path = HISTORY_PATH.search(upstream.body_text()).group(0)
    assert "session_" in path
    upstream.steps.extend(
        [
            Step(text="", tools=[("grep", json.dumps({"pattern": "-MARK", "path": path, "output_mode": "content"}))]),
            Step(text="done"),
            Step(text="done"),
            Step(text="done"),
        ]
    )
    response = server.client.responses.create(input="what did I say?", previous_response_id=left.id)
    output = next(item for item in response.output if item.type == "mcp_call").output

    assert "ROOT0-MARK" in output and "LEFT0-MARK" in output
    assert "RIGHT" not in output


# Chat-completions upstream


@pytest.mark.parametrize(
    "end, reason", [("length", "max_output_tokens"), ("content_filter", "content_filter")]
)
def test_chat_upstream_cutting_the_answer_short_is_incomplete(start_server, upstream, end, reason):
    server = start_server(use_responses_api=False)
    upstream.steps.append(Step(text="partial", end=end))

    response = server.client.responses.create(input="hello")

    assert response.status == "incomplete"
    assert response.incomplete_details.reason == reason
    assert response.completed_at is None
    assert response.output[0].status == "incomplete"


def test_chat_upstream_stream_cut_is_not_reported_as_success(start_server, upstream):
    server = start_server(use_responses_api=False)
    upstream.steps.append(Step(text="half a sent", end="cut"))

    events = list(server.client.responses.create(input="hello", stream=True))

    assert events[-1].type == "response.failed"
    assert events[-1].response.error.code == "upstream_stream_truncated"


# Refusals


def refusal_text(response):
    return [part.refusal for item in response.output for part in item.content]


def test_upstream_refusal_reaches_the_client(server, upstream):
    upstream.steps.append(Step(refusal="I cannot help with that."))

    response = server.client.responses.create(input="hello")

    assert response.status == "completed"
    assert refusal_text(response) == ["I cannot help with that."]
    assert refusal_text(server.client.responses.retrieve(response.id)) == ["I cannot help with that."]


def test_upstream_refusal_survives_the_sdk_stream_accumulator(server, upstream):
    upstream.steps.append(Step(refusal="I cannot help with that."))

    with server.client.responses.stream(model="steve-agent", input="hello") as stream:
        events = list(stream)
        final = stream.get_final_response()

    assert "response.refusal.done" in [event.type for event in events]
    assert refusal_text(final) == ["I cannot help with that."]


def test_a_refusal_can_be_replayed_as_input(server, upstream):
    upstream.steps.append(Step(refusal="I cannot help with that."))
    refused = server.client.responses.create(input="hello")

    replay = [item.model_dump(exclude_none=True) for item in refused.output]
    response = server.client.responses.create(input=[*replay, {"role": "user", "content": "why?"}])

    assert response.status == "completed"
    assert "I cannot help with that." in upstream.body_text()


# Reads and deletes


def walk_ids(server, response_id, **query):
    seen = []
    for item in server.client.responses.input_items.list(response_id, limit=1, **query):
        seen.append(item.content[0].text)
        assert len(seen) <= 10, f"pagination did not end: {seen}"
    return seen


@pytest.mark.parametrize("ids", [["a", "a", "a"], ["a", "a", "b"], ["b", "a", "a"], [7, 7, None]])
def test_input_items_pagination_ends_when_client_ids_repeat(server, ids):
    def message(item_id, text):
        return {"id": item_id, "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}

    response = server.client.responses.create(input=[message(i, t) for i, t in zip(ids, "xyz")])

    assert walk_ids(server, response.id, order="asc") == ["x", "y", "z"]
    assert walk_ids(server, response.id, order="desc") == ["z", "y", "x"]


def test_unique_client_item_ids_are_kept(server):
    item = {"id": "mine", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]}
    response = server.client.responses.create(input=[item])
    assert [i.id for i in server.client.responses.input_items.list(response.id)] == ["mine"]


def test_response_in_flight_has_input_items_and_refuses_delete(server, upstream):
    upstream.steps.append(Step(text="hello there", hold=True))
    stream = server.client.responses.create(input="hi", stream=True)
    response_id = next(iter(stream)).response.id
    assert upstream.holding.wait(10)

    items = server.client.responses.input_items.list(response_id)
    assert [item.content[0].text for item in items] == ["hi"]
    with pytest.raises(openai.BadRequestError) as caught:
        server.client.responses.delete(response_id)
    assert caught.value.code == "response_in_progress"

    upstream.release.set()
    assert list(stream)[-1].type == "response.completed"
    server.client.responses.delete(response_id)
    with pytest.raises(openai.NotFoundError):
        server.client.responses.retrieve(response_id)


def test_store_false_response_in_flight_lists_input_then_is_gone(server, upstream):
    upstream.steps.append(Step(text="hello there", hold=True))
    stream = server.client.responses.create(input="hi", store=False, stream=True)
    response_id = next(iter(stream)).response.id
    assert upstream.holding.wait(10)

    assert len(list(server.client.responses.input_items.list(response_id))) == 1

    upstream.release.set()
    list(stream)
    with pytest.raises(openai.NotFoundError):
        server.client.responses.input_items.list(response_id)


def test_live_child_behaves_as_the_readme_says(server, upstream):
    upstream.routes["ALPHA"] = deque([Step(text="Alpha finished.", hold=True)])
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="Parent done.")])
    stream = iter(server.client.responses.create(input="go", stream=True))
    child_id = next(
        event.item.child_response_id
        for event in stream
        if event.type == "response.output_item.added" and event.item.type == "mcp_call"
    )
    assert upstream.holding.wait(10)

    assert list(server.client.responses.input_items.list(child_id)) == []
    with pytest.raises(openai.BadRequestError) as caught:
        server.client.responses.delete(child_id)
    assert caught.value.code == "child_response_read_only"

    upstream.release.set()
    list(stream)


def test_unknown_response_is_still_not_found(server):
    with pytest.raises(openai.NotFoundError):
        server.client.responses.input_items.list("resp_nope")
    with pytest.raises(openai.NotFoundError):
        server.client.responses.delete("resp_nope")


# Response and error shapes


@pytest.mark.parametrize("stream", [False, True])
def test_completed_response_has_completed_at(server, stream):
    created = server.client.responses.create(input="x", stream=stream)
    response = list(created)[-1].response if stream else created

    assert response.status == "completed"
    assert response.completed_at - response.created_at >= 0
    assert server.client.responses.retrieve(response.id).completed_at == response.completed_at


@pytest.mark.parametrize("body", ['{"input":', "{", "not json"])
def test_malformed_json_has_no_param(server, body):
    response = httpx.post(
        f"{server.url}/v1/responses", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] is None


def test_validation_errors_still_name_the_field(server):
    response = httpx.post(f"{server.url}/v1/responses", json={"input": 5})
    assert response.status_code == 400
    assert response.json()["error"]["param"]


def test_models_retrieve_returns_the_listed_model(server):
    listed = server.client.models.list().data[0]
    assert server.client.models.retrieve(listed.id) == listed


def test_models_retrieve_of_another_model_is_a_not_found_envelope(server):
    with pytest.raises(openai.NotFoundError) as caught:
        server.client.models.retrieve("gpt-nope")
    assert caught.value.code == "model_not_found"


# Serving


def test_first_run_config_is_private_whatever_the_umask(tmp_path, monkeypatch):
    from steve_agent.agent import get_config

    answers = iter(["model", "http://127.0.0.1:9/v1", "secret-key"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    home = tmp_path / "fresh" / "steve"
    previous = os.umask(0o022)
    try:
        config = get_config(home)
    finally:
        os.umask(previous)

    assert config.model.api_key == "secret-key"
    assert stat.S_IMODE((home / "config.yaml").stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "host, warned",
    [("127.0.0.2", True), ("127.0.0.1", False), ("localhost", False), ("::1", False)],
)
def test_serve_warns_when_requests_to_its_address_would_be_rejected(
    tmp_path, monkeypatch, capsys, spy_uvicorn, fake_model, host, warned
):
    config_home(tmp_path, host=host)

    run_cli(monkeypatch, "serve")

    assert ("allowed_hosts" in capsys.readouterr().err) is warned


@pytest.mark.parametrize(
    "server_options",
    [{"host": "127.0.0.2", "allowed_hosts": ["127.0.0.2"]}, {"host": "0.0.0.0", "api_key": "k"}],
)
def test_serve_does_not_warn_for_an_allowed_or_wildcard_address(
    tmp_path, monkeypatch, capsys, spy_uvicorn, fake_model, server_options
):
    config_home(tmp_path, **server_options)
    run_cli(monkeypatch, "serve")

    assert "allowed_hosts" not in capsys.readouterr().err
