"""Retrying failed responses, and the per-conversation memory cap, through the real agent and SDK."""

import json

import openai
import pytest
from fake_upstream import Step

from steve_agent.models import SteveServerConfig

WRITE = ("write_file", json.dumps({"file_path": "/retry_marker.txt", "content": "once"}))


def upstream_500():
    return Step(http_error=(500, "upstream exploded"), no_sdk_retry=True)


def test_defaults():
    assert SteveServerConfig().response_retries == 1
    assert SteveServerConfig().max_conversation_mb == 64


# Retries


def test_failed_model_call_is_retried(start_server, upstream):
    client = start_server(response_retries=1).client
    upstream.steps.extend([upstream_500(), Step(text="Recovered.")])
    response = client.responses.create(input="go")
    assert response.status == "completed"
    assert response.output_text == "Recovered."
    assert len(upstream.requests) == 2


def test_retry_does_not_repeat_the_user_message(start_server, upstream):
    client = start_server(response_retries=1).client
    upstream.steps.extend([upstream_500(), Step(text="Recovered.")])
    client.responses.create(input="UNIQUE_INPUT")
    assert upstream.body_text().count("UNIQUE_INPUT") == 1


def test_zero_retries_disables_it(start_server, upstream):
    client = start_server(response_retries=0).client
    upstream.steps.append(upstream_500())
    with pytest.raises(openai.InternalServerError):
        client.responses.create(input="go")
    assert len(upstream.requests) == 1


def test_gives_up_after_the_configured_retries(start_server, upstream):
    client = start_server(response_retries=2).client
    upstream.steps.extend([upstream_500(), upstream_500(), upstream_500(), Step(text="Too late.")])
    with pytest.raises(openai.InternalServerError):
        client.responses.create(input="go")
    assert len(upstream.requests) == 3


@pytest.mark.parametrize("status", [400, 401, 404])
def test_upstream_refusals_are_not_retried(start_server, upstream, status):
    client = start_server(response_retries=3).client
    upstream.steps.append(Step(http_error=(status, "refused")))
    with pytest.raises(openai.InternalServerError):
        client.responses.create(input="go")
    assert len(upstream.requests) == 1


def test_finished_tools_are_not_run_again(start_server, upstream, steve_home):
    """The point of resuming: this agent writes files."""
    client = start_server(response_retries=1).client
    upstream.steps.extend([Step(text="", tools=[WRITE]), upstream_500(), Step(text="Done.")])
    response = client.responses.create(input="go")

    assert response.output_text == "Done."
    tools = [item for item in response.output if item.type == "mcp_call"]
    assert [(tool.name, tool.status) for tool in tools] == [("write_file", "completed")]
    assert (steve_home / "agents" / "retry_marker.txt").read_text() == "once"
    assert len(upstream.requests) == 3
    # Resumed, not restarted: the model call that recovered already had the tool's result.
    assert "function_call_output" in upstream.body_text()


def test_partial_output_is_not_in_the_final_response(start_server, upstream):
    client = start_server(response_retries=1).client
    upstream.steps.extend([Step(text="Half an ans", end="failed"), Step(text="Whole answer.")])
    plain = client.responses.create(input="go")
    assert plain.output_text == "Whole answer."

    upstream.steps.extend([Step(text="Half an ans", end="failed"), Step(text="Whole answer.")])
    with client.responses.stream(input="go", model="m") as stream:
        events = list(stream)
        final = stream.get_final_response()
    assert final.output_text == "Whole answer."
    assert [item.status for item in final.output] == ["completed"]
    # Streaming clients did see the partial text, and were told that item ended incomplete.
    done = [e.item.status for e in events if e.type == "response.output_item.done"]
    assert done == ["incomplete", "completed"]


def test_cut_off_final_turn_is_retried(start_server, upstream):
    client = start_server(response_retries=1).client
    upstream.steps.extend([Step(text="Cut of", end="cut"), Step(text="Whole answer.")])
    response = client.responses.create(input="UNIQUE_INPUT")
    assert response.status == "completed"
    assert response.output_text == "Whole answer."
    # It went back to before the cut-off turn, so the partial reply is not in the prompt.
    assert "Cut of" not in upstream.body_text()
    assert upstream.body_text().count("UNIQUE_INPUT") == 1


def test_cut_off_turn_with_no_retries_left_fails(start_server, upstream):
    client = start_server(response_retries=1).client
    upstream.steps.extend([Step(text="Cut", end="cut"), Step(text="Cut again", end="cut")])
    with pytest.raises(openai.InternalServerError):
        client.responses.create(input="go")
    assert len(upstream.requests) == 2


def test_incomplete_is_not_a_failure_and_is_not_retried(start_server, upstream):
    client = start_server(response_retries=1).client
    upstream.steps.append(Step(text="Too long", end="incomplete"))
    assert client.responses.create(input="go").status == "incomplete"
    assert len(upstream.requests) == 1


def test_continuing_works_after_a_retry(start_server, upstream):
    client = start_server(response_retries=1).client
    upstream.steps.extend([upstream_500(), Step(text="First answer.")])
    first = client.responses.create(input="FIRST_INPUT")
    upstream.steps.append(Step(text="Second answer."))
    client.responses.create(input="SECOND_INPUT", previous_response_id=first.id)
    body = upstream.body_text()
    assert body.count("FIRST_INPUT") == 1 and "First answer." in body and "SECOND_INPUT" in body


def test_failed_subagent_is_retried_on_the_same_child(start_server, upstream):
    from collections import deque

    client = start_server(response_retries=1).client
    upstream.routes["ALPHA"] = deque([upstream_500(), Step(text="Subagent recovered.")])
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="Parent done.")])

    response = client.responses.create(input="delegate")
    assert response.output_text == "Parent done."
    item = next(i for i in response.output if i.type == "mcp_call" and i.name == "task")
    assert "Subagent recovered." in item.output
    child = client.responses.retrieve(item.child_response_id)
    assert child.status == "completed" and child.output_text == "Subagent recovered."


# Memory cap


def store_of(server):
    return server.app.state.store


def chain(client, upstream, turns, size=200_000):
    """Continue one conversation; the input is unique per turn so it cannot compress away."""
    previous = None
    responses = []
    for turn in range(turns):
        upstream.steps.append(Step(text=f"Answer {turn}."))
        marker = f"INPUT_{turn}_" + "".join(f"{turn}{i}" for i in range(size // 8))
        response = client.responses.create(input=marker[:size], previous_response_id=previous)
        previous = response.id
        responses.append(response)
    return responses


def test_conversation_under_the_cap_is_left_alone(start_server, upstream):
    server = start_server(max_conversation_mb=64)
    responses = chain(server.client, upstream, 3, size=1_000)
    store = store_of(server)
    thread_id = store.get(responses[0].id).thread_id
    assert len(store._conversation(thread_id)) == 3
    assert len(server.client.responses.retrieve(responses[0].id).output) == 1
    assert len(store.channel(responses[0].id).events) > 1


def test_pruning_frees_unused_checkpoints_first_and_keeps_responses(start_server, upstream):
    from collections import deque

    server = start_server(max_conversation_mb=64)
    upstream.routes["ALPHA"] = deque([Step(text="Subagent reply.")])
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="Parent.")])
    first = server.client.responses.create(input="delegate")
    upstream.steps.append(Step(text="Second."))
    second = server.client.responses.create(input="more", previous_response_id=first.id)
    store = store_of(server)
    thread_id = store.get(first.id).thread_id
    saver = store._checkpointer
    assert len(saver.storage[thread_id]) > 1, "the subagent should have left a namespace"
    size = store._conversation_bytes(thread_id)
    events = len(store.channel(first.id).events)

    store._prune_checkpoints(thread_id)

    assert store._conversation_bytes(thread_id) < size
    assert list(saver.storage[thread_id]) == [""]
    assert store.get(first.id) and store.get(second.id)
    assert len(store.channel(first.id).events) == events


def test_history_survives_pruning(start_server, upstream):
    """Pruned checkpoints must not take older messages with them."""
    server = start_server(max_conversation_mb=64)
    responses = chain(server.client, upstream, 3, size=2_000)
    store = store_of(server)
    thread_id = store.get(responses[0].id).thread_id
    store._max_bytes = store._conversation_bytes(thread_id) - 1
    store._prune(thread_id)
    store._max_bytes = 64 * 1024 * 1024

    upstream.steps.append(Step(text="Still going."))
    server.client.responses.create(input="NEXT", previous_response_id=responses[-1].id)
    body = upstream.body_text()
    assert all(f"INPUT_{turn}_" in body for turn in range(3))
    assert all(f"Answer {turn}." in body for turn in range(3))


def test_history_survives_pruning_past_a_channel_snapshot(start_server, upstream):
    """Messages get a stored snapshot every 50 writes; freeing one must not lose what it held."""
    server = start_server(max_conversation_mb=64)
    previous = None
    for turn in range(30):
        upstream.steps.append(Step(text=f"Answer {turn}."))
        previous = server.client.responses.create(input=f"INPUT_{turn}_", previous_response_id=previous).id
    store = store_of(server)
    store._prune_checkpoints(store.get(previous).thread_id)

    upstream.steps.append(Step(text="Still going."))
    server.client.responses.create(input="NEXT", previous_response_id=previous)
    body = upstream.body_text()
    assert all(f"INPUT_{turn}_" in body and f"Answer {turn}." in body for turn in range(30))


def test_old_responses_go_when_compaction_is_not_enough(start_server, upstream):
    server = start_server(max_conversation_mb=64)
    responses = chain(server.client, upstream, 3, size=50_000)
    store = store_of(server)
    thread_id = store.get(responses[0].id).thread_id
    store._max_bytes = 1
    store._prune(thread_id)

    with pytest.raises(openai.NotFoundError):
        server.client.responses.retrieve(responses[0].id)
    assert server.client.responses.retrieve(responses[-1].id).status == "completed"


def test_replay_after_compaction_still_gives_the_whole_response(start_server, upstream):
    server = start_server(max_conversation_mb=64)
    responses = chain(server.client, upstream, 2, size=50_000)
    store = store_of(server)
    store._compact(store.get(responses[0].id))

    events = list(server.client.responses.retrieve(responses[0].id, stream=True))
    assert [event.type for event in events] == ["response.completed"]
    assert events[0].response.output_text == "Answer 0."


def test_conversation_over_the_cap_refuses_more_turns(start_server, upstream):
    server = start_server(max_conversation_mb=0.01)
    first = chain(server.client, upstream, 1, size=50_000)[0]
    with pytest.raises(openai.BadRequestError) as caught:
        server.client.responses.create(input="more", previous_response_id=first.id)
    assert caught.value.code == "conversation_too_large"
    assert len(upstream.requests) == 1
    # It can still be read, and a new conversation still works.
    assert server.client.responses.retrieve(first.id).status == "completed"
    upstream.steps.append(Step(text="Fresh."))
    assert server.client.responses.create(input="new").output_text == "Fresh."


def test_forks_survive_pruning(start_server, upstream):
    server = start_server(max_conversation_mb=64)
    root = chain(server.client, upstream, 1, size=2_000)[0]
    upstream.steps.extend([Step(text="Left."), Step(text="Right.")])
    left = server.client.responses.create(input="LEFT", previous_response_id=root.id)
    right = server.client.responses.create(input="RIGHT", previous_response_id=root.id)
    store = store_of(server)
    thread_id = store.get(root.id).thread_id
    store._max_bytes = store._conversation_bytes(thread_id) - 1
    store._prune(thread_id)
    store._max_bytes = 64 * 1024 * 1024

    upstream.steps.append(Step(text="Left again."))
    server.client.responses.create(input="MORE", previous_response_id=left.id)
    assert "LEFT" in upstream.body_text() and "RIGHT" not in upstream.body_text()
    upstream.steps.append(Step(text="Right again."))
    server.client.responses.create(input="MORE", previous_response_id=right.id)
    assert "RIGHT" in upstream.body_text() and "LEFT" not in upstream.body_text()
