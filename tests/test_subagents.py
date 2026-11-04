"""Watching subagents through the real SDK: child responses of `task` calls, against the real deep agent."""

import json
import threading
import time
from collections import deque

import openai
import pytest
from fake_upstream import Step
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI
from openai.types.responses.response_output_item import McpCall

from steve_agent.server.events import EventTranslator
from steve_agent.server.runner import ChildRuns, RunChannel

LOOK = ("ls", '{"path": "/"}')


def task(description):
    return ("task", json.dumps({"description": description, "subagent_type": "general-purpose"}))


def script_one_task(upstream, first_text=""):
    """The parent delegates ALPHA and then answers. The subagent echoes, without using the parent's steps."""
    upstream.routes["ALPHA"] = deque()
    upstream.steps.extend([Step(text=first_text, tools=[task("ALPHA job")]), Step(text="Parent done.")])


@pytest.fixture
def client(server):
    # A watcher that hangs fails on the read timeout rather than blocking the suite.
    return server.client.with_options(timeout=20)


def read_until_children(stream, count=1):
    """Read the parent stream only as far as `count` child ids, proving they arrive before the subagents end."""
    events, ids = [], []
    for event in stream:
        events.append(event)
        if event.type == "response.output_item.added" and event.item.name == "task":
            ids.append(event.item.child_response_id)
            if len(ids) == count:
                return ids, events
    raise AssertionError(f"Stream ended with only {len(ids)} child ids")


def text_of(events):
    return "".join(event.delta for event in events if event.type == "response.output_text.delta")


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def assert_well_formed(events):
    assert [event.sequence_number for event in events] == list(range(events[0].sequence_number, len(events)))
    assert events[-1].type in ("response.completed", "response.failed", "response.incomplete")
    assert sum(event.type.startswith(("response.completed", "response.failed")) for event in events) == 1


# Exposing the id


def test_child_id_survives_the_sdk_on_added_done_and_final(client, upstream):
    upstream.routes["ALPHA"] = deque()
    upstream.steps.extend([Step(text="", tools=[task("ALPHA job"), LOOK]), Step(text="Parent done.")])

    events = list(client.responses.create(input="go", stream=True))

    def items(event_type):
        return {e.item.name: e.item for e in events if e.type == event_type and isinstance(e.item, McpCall)}

    added, done = items("response.output_item.added"), items("response.output_item.done")
    child_id = added["task"].child_response_id
    assert child_id.startswith("resp_")
    assert done["task"].child_response_id == child_id
    assert not hasattr(added["ls"], "child_response_id") and not hasattr(done["ls"], "child_response_id")

    final = events[-1].response.output[0]
    assert final.child_response_id == child_id
    assert client.responses.retrieve(events[0].response.id).output[0].child_response_id == child_id


def test_child_id_survives_the_stream_helper(client, upstream):
    script_one_task(upstream)

    with client.responses.stream(input="go", model="steve-agent") as stream:
        events = list(stream)
        final = stream.get_final_response()

    added = next(e for e in events if e.type == "response.output_item.added" and e.item.type == "mcp_call")
    assert added.item.child_response_id == final.output[0].child_response_id
    assert final.output[0].child_response_id.startswith("resp_")


# Watching


def test_watch_one_subagent_live_with_its_text_and_tools(client, upstream):
    upstream.routes["ALPHA"] = deque([Step(text="Looking now.", tools=[LOOK], hold=True), Step(text="Alpha finished.")])
    upstream.steps.extend([Step(text="", tools=[task("ALPHA job")]), Step(text="Parent done.")])

    parent = client.responses.create(input="go", stream=True)
    (child_id,), parent_events = read_until_children(parent)

    events = []
    for event in client.responses.retrieve(child_id, stream=True):
        events.append(event)
        # The subagent is held until the watcher has seen its first token, so this can only pass live.
        if event.type == "response.output_text.delta":
            upstream.release.set()
    assert_well_formed(events)

    final = events[-1].response
    assert events[-1].type == "response.completed"
    assert final.id == child_id
    assert [item.type for item in final.output] == ["message", "mcp_call", "message"]
    assert text_of(events) == "Looking now.Alpha finished."
    tool = final.output[1]
    assert (tool.name, tool.arguments, tool.status) == ("ls", '{"path": "/"}', "completed")
    assert "AGENTS.md" in tool.output
    types = [event.type for event in events]
    assert types[:2] == ["response.created", "response.in_progress"]
    assert "response.mcp_call_arguments.done" in types and "response.mcp_call.completed" in types

    parent_events += list(parent)
    assert parent_events[-1].response.output_text == "Parent done."
    assert parent_events[-1].response.output[0].output == "Alpha finished."


def test_two_parallel_subagents_stay_separate(client, upstream):

    both = threading.Barrier(2)
    upstream.routes["ALPHA"] = deque(
        [Step(text="alpha-looking", tools=[LOOK], barrier=both), Step(text="alpha-finished")]
    )
    upstream.routes["BETA"] = deque(
        [
            Step(text="beta-looking", tools=[("read_file", '{"file_path": "/nope"}')], barrier=both),
            Step(text="beta-finished"),
        ]
    )
    upstream.steps.extend([Step(text="", tools=[task("ALPHA job"), task("BETA job")]), Step(text="Parent done.")])

    parent = client.responses.create(input="go", stream=True)
    ids, parent_events = read_until_children(parent, 2)
    watched = {child_id: list(client.responses.retrieve(child_id, stream=True)) for child_id in ids}
    parent_events += list(parent)

    # Identify each child by the description in the parent's item, never by position.
    final = parent_events[-1].response
    by_job = {json.loads(item.arguments)["description"]: item for item in final.output if item.type == "mcp_call"}
    alpha = watched[by_job["ALPHA job"].child_response_id]
    beta = watched[by_job["BETA job"].child_response_id]

    assert text_of(alpha) == "alpha-lookingalpha-finished"
    assert text_of(beta) == "beta-lookingbeta-finished"
    assert by_job["ALPHA job"].output == "alpha-finished"
    assert by_job["BETA job"].output == "beta-finished"
    for events, tool_name in ((alpha, "ls"), (beta, "read_file")):
        assert_well_formed(events)
        tools = [item for item in events[-1].response.output if item.type == "mcp_call"]
        assert [item.name for item in tools] == [tool_name]

    assert final.output_text == "Parent done."
    assert text_of(parent_events) == "Parent done."

    # Parent usage still counts everything, and each child counts only its own calls.
    calls = len(upstream.requests)
    assert calls == 6
    assert final.usage.total_tokens == sum(100 * n + 20 for n in range(1, calls + 1))
    parent_calls = (100 + 20) + (100 * calls + 20)
    child_total = sum(events[-1].response.usage.total_tokens for events in (alpha, beta))
    assert child_total == final.usage.total_tokens - parent_calls


# Replay and snapshots


def test_late_subscriber_replays_during_the_run(client, upstream):

    upstream.routes["ALPHA"] = deque([Step(text="Alpha finished.", hold=True)])
    upstream.steps.extend([Step(text="", tools=[task("ALPHA job")]), Step(text="Parent done.")])

    parent = client.responses.create(input="go", stream=True)
    (child_id,), parent_events = read_until_children(parent)
    assert upstream.holding.wait(10)
    wait_for(lambda: client.responses.retrieve(child_id).output)

    # The first token went out before this subscriber arrived; it still gets everything, then the rest live.
    late = client.responses.retrieve(child_id, stream=True)
    first = next(iter(late))
    assert first.type == "response.created" and first.sequence_number == 0
    upstream.release.set()
    events = [first, *late]
    assert_well_formed(events)
    assert text_of(events) == "Alpha finished."
    list(parent)


def test_replay_after_the_run_for_child_and_parent(client, upstream):
    script_one_task(upstream)
    parent_events = list(client.responses.create(input="go", stream=True))
    parent_id = parent_events[-1].response.id
    child_id = parent_events[-1].response.output[0].child_response_id

    child_events = list(client.responses.retrieve(child_id, stream=True))
    assert_well_formed(child_events)
    assert text_of(child_events) == "echo: ALPHA job"

    assert list(client.responses.retrieve(parent_id, stream=True)) == parent_events
    for response_id, events in ((child_id, child_events), (parent_id, parent_events)):
        tail = list(client.responses.retrieve(response_id, stream=True, starting_after=2))
        assert tail == events[3:]
        # Past the end must close, not wait for events that will never come.
        terminal = events[-1].sequence_number
        assert list(client.responses.retrieve(response_id, stream=True, starting_after=terminal)) == []
        assert list(client.responses.retrieve(response_id, stream=True, starting_after=terminal + 5)) == []


def test_parent_can_be_watched_during_its_run(client, upstream):

    upstream.routes["ALPHA"] = deque([Step(text="Alpha finished.", hold=True)])
    upstream.steps.extend([Step(text="", tools=[task("ALPHA job")]), Step(text="Parent done.")])

    parent = client.responses.create(input="go", stream=True)
    (child_id,), parent_events = read_until_children(parent)
    assert upstream.holding.wait(10)
    parent_id = parent_events[0].response.id

    snapshot = client.responses.retrieve(parent_id)
    assert snapshot.status == "in_progress"
    assert snapshot.output[0].child_response_id == child_id

    watcher = client.responses.retrieve(parent_id, stream=True)
    assert next(iter(watcher)) == parent_events[0]
    upstream.release.set()
    assert list(watcher)[-1].response.output_text == "Parent done."
    list(parent)


def test_snapshot_polling_during_and_after(client, upstream):

    upstream.routes["ALPHA"] = deque([Step(text="Alpha finished.", hold=True)])
    upstream.steps.extend([Step(text="", tools=[task("ALPHA job")]), Step(text="Parent done.")])

    parent = client.responses.create(input="go", stream=True)
    (child_id,), _ = read_until_children(parent)

    assert client.responses.retrieve(child_id).id == child_id
    wait_for(lambda: client.responses.retrieve(child_id).output_text == "Alpha")
    snapshot = client.responses.retrieve(child_id)
    assert snapshot.status == "in_progress"
    assert snapshot.output[0].status == "in_progress"

    upstream.release.set()
    list(parent)
    final = client.responses.retrieve(child_id)
    assert final.status == "completed"
    assert final.output_text == "Alpha finished."
    assert final.usage.total_tokens > 0
    assert client.responses.input_items.list(child_id).data == []


# Ending with the parent


def test_parent_cancelled_mid_subagent_ends_the_watcher(client, upstream):

    upstream.routes["ALPHA"] = deque([Step(text="Alpha finished.", hold=True)])
    upstream.steps.append(Step(text="", tools=[task("ALPHA job")]))

    parent = client.responses.create(input="go", stream=True)
    (child_id,), parent_events = read_until_children(parent)
    assert upstream.holding.wait(10)
    watcher = client.responses.retrieve(child_id, stream=True)
    first = next(iter(watcher))

    parent.close()

    events = [first, *watcher]
    assert_well_formed(events)
    assert events[-1].type == "response.failed"
    assert events[-1].response.status == "cancelled"
    assert events[-1].response.error.code == "cancelled"
    assert upstream.hung_up.wait(10)

    # Cancelled runs are not stored, and that includes their children.
    for response_id in (child_id, parent_events[0].response.id):
        with pytest.raises(openai.NotFoundError):
            client.responses.retrieve(response_id)


def test_parent_failure_ends_every_child(client, upstream):

    both = threading.Barrier(2)
    upstream.routes["ALPHA"] = deque([Step(text="Alpha finished.", hold=True, barrier=both)])
    upstream.routes["BETA"] = deque([Step(http_error=(400, "boom sk-upstream-secret"), barrier=both)])
    upstream.steps.append(Step(text="", tools=[task("ALPHA job"), task("BETA job")]))

    parent = client.responses.create(input="go", stream=True)
    ids, parent_events = read_until_children(parent, 2)
    watched = [list(client.responses.retrieve(child_id, stream=True)) for child_id in ids]
    parent_events += list(parent)

    assert parent_events[-1].type == "response.failed"
    assert parent_events[-1].response.status == "failed"
    for events in watched:
        assert_well_formed(events)
        assert events[-1].type == "response.failed"
        assert events[-1].response.status == "failed"
        assert "sk-upstream-secret" not in events[-1].model_dump_json(warnings=False)
    # A failed response is stored, so its children can still be read.
    assert client.responses.retrieve(ids[0]).status == "failed"


def test_task_rejected_before_any_subagent_ran_fails_only_the_child(client, upstream):
    upstream.steps.extend([Step(text="", tools=[("task", '{"description": "no type"}')]), Step(text="Parent done.")])

    final = client.responses.create(input="go")

    assert final.status == "completed"
    assert final.output[0].status == "failed"
    child = client.responses.retrieve(final.output[0].child_response_id)
    assert child.status == "failed"
    assert child.output == []


def test_truncated_subagent_fails_its_child_and_not_the_parent(client, upstream):

    upstream.routes["ALPHA"] = deque([Step(text="Partial", end="cut")])
    upstream.steps.extend([Step(text="", tools=[task("ALPHA job")]), Step(text="Parent done.")])

    final = client.responses.create(input="go")

    assert final.status == "completed"
    child = client.responses.retrieve(final.output[0].child_response_id)
    assert child.status == "failed"
    assert child.error.code == "upstream_stream_truncated"


# Lifetimes and store: false


def test_store_false_children_are_watchable_only_during_the_run(client, upstream):

    upstream.routes["ALPHA"] = deque([Step(text="Alpha finished.", hold=True)])
    upstream.steps.extend([Step(text="", tools=[task("ALPHA job")]), Step(text="Parent done.")])

    parent = client.responses.create(input="go", stream=True, store=False)
    (child_id,), parent_events = read_until_children(parent)

    events = []
    for event in client.responses.retrieve(child_id, stream=True):
        events.append(event)
        if event.type == "response.output_text.delta":
            upstream.release.set()
    assert events[-1].type == "response.completed"
    assert events[-1].response.store is False
    parent_events += list(parent)

    for response_id in (child_id, parent_events[0].response.id):
        with pytest.raises(openai.NotFoundError):
            client.responses.retrieve(response_id)
        with pytest.raises(openai.NotFoundError):
            list(client.responses.retrieve(response_id, stream=True))


def test_children_are_read_only(client, upstream):

    upstream.routes["ALPHA"] = deque([Step(text="Alpha finished.", hold=True)])
    upstream.steps.extend([Step(text="", tools=[task("ALPHA job")]), Step(text="Parent done.")])

    parent = client.responses.create(input="go", stream=True)
    (child_id,), parent_events = read_until_children(parent)
    assert upstream.holding.wait(10)

    calls = len(upstream.requests)
    with pytest.raises(openai.BadRequestError):  # in flight
        client.responses.create(input="more", previous_response_id=child_id)

    upstream.release.set()
    parent_events += list(parent)
    with pytest.raises(openai.BadRequestError) as stored:
        client.responses.create(input="more", previous_response_id=child_id)
    assert stored.value.body["param"] == "previous_response_id"
    assert len(upstream.requests) == calls + 1  # only the parent's own next turn; the rejected ones never ran

    with pytest.raises(openai.BadRequestError):
        client.responses.delete(child_id)
    assert client.responses.retrieve(child_id).status == "completed"

    # The parent can still be continued.
    upstream.steps.append(Step(text="Continued."))
    next_turn = client.responses.create(input="more", previous_response_id=parent_events[-1].response.id)
    assert next_turn.output_text == "Continued."


def test_deleting_the_parent_removes_its_children(client, upstream):
    script_one_task(upstream)
    final = client.responses.create(input="go")
    child_id = final.output[0].child_response_id
    assert client.responses.retrieve(child_id).status == "completed"

    client.responses.delete(final.id)

    with pytest.raises(openai.NotFoundError):
        client.responses.retrieve(child_id)


def test_evicting_the_parent_removes_its_children(start_server, upstream):
    client = start_server(max_conversations=1).client
    script_one_task(upstream)
    final = client.responses.create(input="go")
    child_id = final.output[0].child_response_id
    assert client.responses.retrieve(child_id).status == "completed"

    client.responses.create(input="another conversation")

    for response_id in (final.id, child_id):
        with pytest.raises(openai.NotFoundError):
            client.responses.retrieve(response_id)


# No observer


def test_no_observer_sees_the_same_parent_as_before(client, upstream):
    script_one_task(upstream, "Delegating.")

    events = list(client.responses.create(input="go", stream=True))

    assert [event.type for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.output_item.added",
        "response.mcp_call_arguments.delta",
        "response.mcp_call_arguments.done",
        "response.mcp_call.in_progress",
        "response.mcp_call.completed",
        "response.output_item.done",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [event.sequence_number for event in events] == list(range(len(events)))
    assert text_of(events) == "Delegating.Parent done."
    final = events[-1].response
    assert final.status == "completed"
    tool = final.output[1]
    # The reference is the only addition to the tool item, and it adds no extra upstream calls.
    assert set(tool.model_dump(exclude_none=True)) == {
        "id", "arguments", "name", "server_label", "type", "output", "status", "child_response_id",
    }  # fmt: skip
    assert tool.output == "echo: ALPHA job"
    assert len(upstream.requests) == 3


def test_non_streaming_parent_matches_and_children_are_stored(client, upstream):
    script_one_task(upstream)

    final = client.responses.create(input="go")

    assert final.output_text == "Parent done."
    assert client.responses.retrieve(final.output[0].child_response_id).output_text == "echo: ALPHA job"


def test_chat_openai_client_is_not_disturbed_by_the_child_reference(server, upstream):
    script_one_task(upstream)
    model = ChatOpenAI(model="steve-agent", base_url=f"{server.url}/v1", api_key="unused", use_responses_api=True)

    assert model.invoke("go").text == "Parent done."


# Units


def subagent_chunk(ns, text):
    metadata = {"langgraph_node": "model", "langgraph_checkpoint_ns": f"{ns}|model:1", "lc_agent_name": "gp"}
    return (ns,), AIMessageChunk(content=text), metadata


def test_translator_with_a_namespace_reports_only_that_subagent():
    translator = EventTranslator(namespace=("tools:a",))
    translator.start()

    assert translator.feed(*subagent_chunk("tools:b", "nope")) == []
    events = translator.feed(*subagent_chunk("tools:a", "yes"))

    assert [event.delta for event in events if event.type == "response.output_text.delta"] == ["yes"]


def test_child_is_routed_whichever_of_open_and_bind_comes_first():
    children = ChildRuns()
    opened_first = children.open("call_1")
    children.bind("tools:a", "call_1")
    children.bind("tools:b", "call_2")
    bound_first = children.open("call_2")

    children.feed(*subagent_chunk("tools:b", "for b"))

    by_id = {child.translator.response_id: child for child in children}
    assert set(by_id) == {opened_first, bound_first}
    assert text_of(by_id[bound_first].channel.events) == "for b"
    assert text_of(by_id[opened_first].channel.events) == ""


def test_chunks_from_an_unbound_namespace_go_nowhere():
    children = ChildRuns()
    response_id = children.open("call_1")

    children.feed(*subagent_chunk("tools:unknown", "stray"))

    child = next(iter(children))
    assert child.translator.response_id == response_id
    assert text_of(child.channel.events) == ""


async def test_replay_at_or_past_the_end_closes_instead_of_waiting():
    translator = EventTranslator()
    channel = RunChannel()
    channel.publish(translator.start() + translator.finish())
    terminal = channel.events[-1].sequence_number

    assert [event async for event in channel.subscribe(terminal)] == []
    assert [event async for event in channel.subscribe(terminal + 5)] == []
