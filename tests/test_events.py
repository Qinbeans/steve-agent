"""EventTranslator fed with real message objects, using chunk shapes observed from the deep agent."""

import json

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from openai._models import construct_type
from openai._types import omit
from openai.lib.streaming.responses import ResponseStreamState
from openai.types.responses import ResponseStreamEvent

from steve_agent.server.events import EventTranslator

USAGE_1 = {
    "input_tokens": 100,
    "output_tokens": 20,
    "total_tokens": 120,
    "input_token_details": {"cache_read": 3},
    "output_token_details": {"reasoning": 2},
}
USAGE_2 = {**USAGE_1, "input_tokens": 200, "total_tokens": 220}


def model_md(turn=1, **extra):
    return {"langgraph_node": "model", "langgraph_checkpoint_ns": f"model:{turn}", **extra}


def tools_md():
    return {"langgraph_node": "tools", "langgraph_checkpoint_ns": "tools:1"}


def reasoning(text, part=0, block=0):
    summary = [{"index": part, "type": "summary_text", "text": text}]
    return AIMessageChunk(content=[{"summary": summary, "index": block, "type": "reasoning"}])


def reasoning_start(block=0):
    return AIMessageChunk(content=[{"id": "rs_1", "summary": [], "type": "reasoning", "index": block}])


def encrypted(blob, block=0):
    return AIMessageChunk(
        content=[{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": blob, "index": block}]
    )


def text(delta, block=1):
    return AIMessageChunk(content=[{"type": "text", "text": delta, "index": block}])


def text_end(block=1):
    return AIMessageChunk(content=[{"type": "text", "text": "", "id": "msg_upstream", "index": block}])


def tool_start(name, call_id, block=2):
    return AIMessageChunk(
        content=[{"type": "function_call", "name": name, "arguments": "", "call_id": call_id, "index": block}],
        tool_call_chunks=[{"name": name, "args": "", "id": call_id, "index": block, "type": "tool_call_chunk"}],
    )


def tool_args(delta, block=2):
    return AIMessageChunk(
        content=[{"type": "function_call", "arguments": delta, "index": block}],
        tool_call_chunks=[{"name": None, "args": delta, "id": None, "index": block, "type": "tool_call_chunk"}],
    )


def last(status="completed", usage=USAGE_1, **metadata):
    response_metadata = {"model_provider": "openai", **metadata}
    if status:
        response_metadata["status"] = status
    return AIMessageChunk(
        content=[], chunk_position="last", usage_metadata=usage, response_metadata=response_metadata
    )


def tool_result(call_id, content="['/a']", status="success", name="ls"):
    return ToolMessage(content=content, tool_call_id=call_id, name=name, status=status)


def check_stream(events):
    """Invariants for every stream, then the SDK accumulator on the events as a client would parse them."""
    numbers = [event.sequence_number for event in events]
    assert numbers == sorted(set(numbers))

    added = [event for event in events if event.type == "response.output_item.added"]
    done = [event for event in events if event.type == "response.output_item.done"]
    indexes = [event.output_index for event in added]
    assert indexes == list(range(len(added)))
    assert sorted(event.output_index for event in done) == indexes

    terminal = events[-1]
    assert terminal.type in ("response.completed", "response.incomplete", "response.failed")
    assert [event.type for event in events[:2]] == ["response.created", "response.in_progress"]
    assert [item.id for item in terminal.response.output] == [event.item.id for event in added]
    done_by_index = {event.output_index: event.item for event in done}
    assert [item.model_dump() for item in terminal.response.output] == [
        done_by_index[index].model_dump() for index in indexes
    ]
    for item in terminal.response.output:
        assert item.type in ("reasoning", "message", "mcp_call")
        assert item.status != "in_progress"

    state = ResponseStreamState(input_tools=omit, text_format=omit)
    for event in events:
        wire = json.loads(event.model_dump_json())
        state.handle_event(construct_type(type_=ResponseStreamEvent, value=wire))


def run(script, end="finish", **kwargs):
    """script is a list of (chunk, metadata) or (ns, chunk, metadata)."""
    translator = EventTranslator(**kwargs)
    events = translator.start()
    for step in script:
        ns, chunk, metadata = ((), *step) if len(step) == 2 else step
        events += translator.feed(ns, chunk, metadata)
    events += getattr(translator, end)()
    check_stream(events)
    return events


def types(events):
    return [event.type.removeprefix("response.") for event in events]


def by_type(events, event_type):
    return [event for event in events if event.type == f"response.{event_type}"]


def output_text(response):
    return [item.content[0].text for item in response.output if item.type == "message"]


TWO_TURNS = [
    (AIMessageChunk(content=[], id="resp_1"), model_md()),
    (reasoning_start(), model_md()),
    (reasoning("Thinki"), model_md()),
    (reasoning("ng A."), model_md()),
    (reasoning("Thinking B.", part=1), model_md()),
    (text("Let m"), model_md()),
    (text("e look."), model_md()),
    (text_end(), model_md()),
    (tool_start("ls", "call_1"), model_md()),
    (tool_args('{"pa'), model_md()),
    (tool_args('th": "/"}'), model_md()),
    (last(), model_md()),
    (tool_result("call_1"), tools_md()),
    (AIMessageChunk(content=[], id="resp_2"), model_md(2)),
    (text("All d", block=0), model_md(2)),
    (text("one.", block=0), model_md(2)),
    (text_end(block=0), model_md(2)),
    (last(usage=USAGE_2), model_md(2)),
]


def test_start_emits_created_then_in_progress():
    created, in_progress = EventTranslator().start()
    assert created.type == "response.created"
    assert in_progress.type == "response.in_progress"
    assert created.sequence_number == 0 and in_progress.sequence_number == 1
    assert created.response.status == "in_progress"
    assert created.response.id == in_progress.response.id


def test_reasoning_text_tool_text_over_two_turns():
    events = run(TWO_TURNS)
    assert types(events) == [
        "created",
        "in_progress",
        "output_item.added",
        "reasoning_summary_part.added",
        "reasoning_summary_text.delta",
        "reasoning_summary_text.delta",
        "reasoning_summary_text.done",
        "reasoning_summary_part.done",
        "reasoning_summary_part.added",
        "reasoning_summary_text.delta",
        "reasoning_summary_text.done",
        "reasoning_summary_part.done",
        "output_item.done",
        "output_item.added",
        "content_part.added",
        "output_text.delta",
        "output_text.delta",
        "output_text.done",
        "content_part.done",
        "output_item.done",
        "output_item.added",
        "mcp_call_arguments.delta",
        "mcp_call_arguments.delta",
        "mcp_call_arguments.done",
        "mcp_call.in_progress",
        "mcp_call.completed",
        "output_item.done",
        "output_item.added",
        "content_part.added",
        "output_text.delta",
        "output_text.delta",
        "output_text.done",
        "content_part.done",
        "output_item.done",
        "completed",
    ]
    response = events[-1].response
    assert response.status == "completed"
    assert [item.type for item in response.output] == ["reasoning", "message", "mcp_call", "message"]
    rs, first, tool, second = response.output
    assert [part.text for part in rs.summary] == ["Thinking A.", "Thinking B."]
    assert first.content[0].text == "Let me look."
    assert second.content[0].text == "All done."
    assert (tool.name, tool.arguments, tool.output) == ("ls", '{"path": "/"}', "['/a']")
    assert tool.server_label == "steve"
    assert tool.status == "completed" and tool.error is None
    assert (rs.id[:3], first.id[:4], tool.id[:4]) == ("rs_", "msg_", "mcp_")
    assert "msg_upstream" not in events[-1].model_dump_json()


def test_text_events_carry_the_required_fields():
    events = run(TWO_TURNS)
    delta = by_type(events, "output_text.delta")[0]
    assert delta.logprobs == [] and delta.content_index == 0
    assert by_type(events, "output_text.done")[0].text == "Let me look."
    assert by_type(events, "content_part.done")[0].part.text == "Let me look."
    summary_done = by_type(events, "reasoning_summary_text.done")
    assert [(event.summary_index, event.text) for event in summary_done] == [(0, "Thinking A."), (1, "Thinking B.")]
    assert by_type(events, "reasoning_summary_part.done")[1].part.text == "Thinking B."
    assert by_type(events, "mcp_call_arguments.done")[0].arguments == '{"path": "/"}'


def test_earlier_events_are_not_changed_by_later_ones():
    events = run(TWO_TURNS)
    assert by_type(events, "created")[0].response.output == []
    added = by_type(events, "output_item.added")
    assert added[0].item.summary == [] and added[0].item.status == "in_progress"
    assert added[1].item.content == [] and added[1].item.status == "in_progress"
    assert added[2].item.arguments == "" and added[2].item.output is None
    assert by_type(events, "content_part.added")[0].part.text == ""


def test_accumulator_is_a_real_check_of_event_order():
    events = run(TWO_TURNS)
    broken = [event for event in events if event.type != "response.output_item.added"]
    state = ResponseStreamState(input_tools=omit, text_format=omit)
    with pytest.raises(RuntimeError, match="before receiving its output item"):
        for event in broken:
            state.handle_event(event)


def test_accumulator_final_response_matches_the_terminal_output():
    events = run(TWO_TURNS)
    state = ResponseStreamState(input_tools=omit, text_format=omit)
    for event in events[:-1]:
        state.handle_event(event)
    final = state.handle_event(events[-1])[-1].response
    assert final.output_text == "Let me look.All done."
    assert [item.type for item in final.output] == ["reasoning", "message", "mcp_call", "message"]


def test_summarization_tokens_are_excluded_but_counted():
    # Summarization shares node and checkpoint_ns with the real call.
    summary_md = model_md(lc_source="summarization")
    script = [
        (text("SUMMARY", block=0), summary_md),
        (last(usage=USAGE_1), summary_md),
        (text("real", block=0), model_md()),
        (last(usage=USAGE_2), model_md()),
    ]
    events = run(script)
    response = events[-1].response
    assert output_text(response) == ["real"]
    assert "SUMMARY" not in json.dumps([event.model_dump(mode="json") for event in events])
    assert response.usage.input_tokens == 300
    assert response.usage.total_tokens == 340


def test_subagent_tokens_and_tools_are_excluded_but_counted():
    ns = ("tools:abc",)
    sub_model = model_md(lc_agent_name="general-purpose")
    sub_model["langgraph_checkpoint_ns"] = "tools:abc|model:9"
    sub_tools = {"langgraph_node": "tools", "langgraph_checkpoint_ns": "tools:abc|tools:7"}
    script = [
        (tool_start("task", "call_task"), model_md()),
        (tool_args('{"description": "x"}'), model_md()),
        (last(), model_md()),
        (ns, reasoning("SUBREASON"), sub_model),
        (ns, text("SUBTEXT", block=0), sub_model),
        (ns, tool_start("ls", "call_sub"), sub_model),
        (ns, last(usage=USAGE_2), sub_model),
        (ns, tool_result("call_sub", content="SUBTOOL"), sub_tools),
        (tool_result("call_task", content="subagent answer", name="task"), tools_md()),
        (text("done", block=0), model_md(2)),
        (last(), model_md(2)),
    ]
    events = run(script)
    response = events[-1].response
    serialized = json.dumps([event.model_dump(mode="json") for event in events])
    for leaked in ("SUBREASON", "SUBTEXT", "SUBTOOL", "call_sub"):
        assert leaked not in serialized
    assert [item.type for item in response.output] == ["mcp_call", "message"]
    assert response.output[0].name == "task"
    assert response.output[0].output == "subagent answer"
    assert response.usage.input_tokens == 400


@pytest.mark.parametrize(
    "ns, checkpoint_ns",
    [
        pytest.param((), "tools:abc|model:9", id="nested-checkpoint-ns"),
        pytest.param(("tools:abc",), "model:9", id="subgraph-ns"),
    ],
)
def test_either_subagent_marker_alone_excludes_a_chunk(ns, checkpoint_ns):
    sub_model = {**model_md(), "langgraph_checkpoint_ns": checkpoint_ns}
    script = [(ns, text("SUB", block=0), sub_model), (text("top", block=0), model_md()), (last(), model_md())]
    assert output_text(run(script)[-1].response) == ["top"]


def test_subagent_turn_does_not_decide_the_status_or_close_the_parent_turn():
    ns = ("tools:abc",)
    sub_model = {**model_md(), "langgraph_checkpoint_ns": "tools:abc|model:9"}
    script = [
        (text("par", block=0), model_md()),
        (ns, last(status="incomplete"), sub_model),
        (text("ent", block=0), model_md()),
        (last(), model_md()),
    ]
    events = run(script)
    assert events[-1].type == "response.completed"
    assert output_text(events[-1].response) == ["parent"]


def test_parallel_tool_calls_stay_open_and_keep_their_order():
    script = [
        (tool_start("ls", "call_a", block=2), model_md()),
        (tool_args('{"a": 1}', block=2), model_md()),
        (tool_start("glob", "call_b", block=3), model_md()),
        (tool_args('{"b": 2}', block=3), model_md()),
        (last(), model_md()),
        (tool_result("call_b", content="from b", name="glob"), tools_md()),
        (tool_result("call_a", content="from a"), tools_md()),
        (text("done", block=0), model_md(2)),
        (last(), model_md(2)),
    ]
    events = run(script)
    assert types(events)[2:14] == [
        "output_item.added",
        "mcp_call_arguments.delta",
        "output_item.added",
        "mcp_call_arguments.delta",
        "mcp_call_arguments.done",
        "mcp_call.in_progress",
        "mcp_call_arguments.done",
        "mcp_call.in_progress",
        "mcp_call.completed",
        "output_item.done",
        "mcp_call.completed",
        "output_item.done",
    ]
    a, b, message = events[-1].response.output
    assert (a.name, a.arguments, a.output) == ("ls", '{"a": 1}', "from a")
    assert (b.name, b.arguments, b.output) == ("glob", '{"b": 2}', "from b")
    assert message.type == "message"
    completed = by_type(events, "mcp_call.completed")
    assert [event.output_index for event in completed] == [1, 0]


def test_interleaved_tool_arguments_go_to_the_right_item():
    script = [
        (tool_start("ls", "call_a", block=2), model_md()),
        (tool_start("glob", "call_b", block=3), model_md()),
        (tool_args("A1", block=2), model_md()),
        (tool_args("B1", block=3), model_md()),
        (tool_args("A2", block=2), model_md()),
        (last(), model_md()),
        (tool_result("call_a"), tools_md()),
        (tool_result("call_b"), tools_md()),
    ]
    a, b = run(script + [(text("x", block=0), model_md(2)), (last(), model_md(2))])[-1].response.output[:2]
    assert (a.arguments, b.arguments) == ("A1A2", "B1")


def test_tool_error_becomes_a_failed_mcp_call():
    script = [
        (tool_start("read_file", "call_1"), model_md()),
        (tool_args("{}"), model_md()),
        (last(), model_md()),
        (tool_result("call_1", content="Error: no such file", status="error", name="read_file"), tools_md()),
        (text("sorry", block=0), model_md(2)),
        (last(), model_md(2)),
    ]
    events = run(script)
    assert len(by_type(events, "mcp_call.failed")) == 1
    assert by_type(events, "mcp_call.completed") == []
    assert events[-1].type == "response.completed"
    tool = events[-1].response.output[0]
    assert tool.status == "failed"
    assert tool.output is None
    assert tool.error.type == "mcp_tool_execution_error"
    assert tool.error.content == [{"type": "text", "text": "Error: no such file"}]


def test_tool_output_that_is_not_a_string_is_stringified():
    blocks = [{"type": "text", "text": "hello"}]
    script = [
        (tool_start("ls", "call_1"), model_md()),
        (last(), model_md()),
        (ToolMessage(content=blocks, tool_call_id="call_1", status="success"), tools_md()),
        (text("x", block=0), model_md(2)),
        (last(), model_md(2)),
    ]
    tool = run(script)[-1].response.output[0]
    assert json.loads(tool.output) == blocks


def test_tool_message_for_an_unknown_call_is_not_emitted():
    script = [
        (tool_result("call_never_seen", content="ORPHAN"), tools_md()),
        (text("x", block=0), model_md()),
        (last(), model_md()),
    ]
    events = run(script)
    assert "ORPHAN" not in json.dumps([event.model_dump(mode="json") for event in events])
    assert [item.type for item in events[-1].response.output] == ["message"]


def test_plain_string_content_from_chat_completions():
    chat_last = AIMessageChunk(content="", chunk_position="last", response_metadata={"finish_reason": "stop"})
    tool = AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": "ls", "args": "{}", "id": "call_1", "index": 0, "type": "tool_call_chunk"}],
    )
    script = [
        (AIMessageChunk(content="Let "), model_md()),
        (AIMessageChunk(content="me look."), model_md()),
        (tool, model_md()),
        (chat_last, model_md()),
        (tool_result("call_1"), tools_md()),
        (AIMessageChunk(content="Done."), model_md(2)),
        (chat_last, model_md(2)),
    ]
    events = run(script)
    response = events[-1].response
    assert response.status == "completed"
    assert [item.type for item in response.output] == ["message", "mcp_call", "message"]
    assert output_text(response) == ["Let me look.", "Done."]
    assert all(item.status == "completed" for item in response.output)
    assert response.usage is None


def test_encrypted_reasoning_is_never_exposed():
    blob = "ENCRYPTED_BLOB_1"
    script = [
        (reasoning_start(), model_md()),
        (reasoning("Visible."), model_md()),
        (encrypted(blob), model_md()),
        (text("hi"), model_md()),
        (last(), model_md()),
    ]
    events = run(script)
    assert blob not in "".join(event.model_dump_json() for event in events)
    rs = events[-1].response.output[0]
    assert rs.encrypted_content is None
    assert rs.summary[0].text == "Visible."


def test_encrypted_only_reasoning_produces_no_item():
    script = [
        (reasoning_start(), model_md()),
        (reasoning(""), model_md()),
        (encrypted("ENCRYPTED_BLOB_1"), model_md()),
        (text("hi"), model_md()),
        (last(), model_md()),
    ]
    events = run(script)
    assert [item.type for item in events[-1].response.output] == ["message"]
    assert by_type(events, "reasoning_summary_part.added") == []


def test_turn_boundary_ignores_chunk_ids_and_uses_the_checkpoint_ns():
    # Same id across turns, different id inside a turn, and no last chunk on the first turn.
    script = [
        (AIMessageChunk(content=[{"type": "text", "text": "a", "index": 0}], id="x"), model_md(1)),
        (AIMessageChunk(content=[{"type": "text", "text": "b", "index": 0}], id="y"), model_md(1)),
        (AIMessageChunk(content=[{"type": "text", "text": "c", "index": 0}], id="y"), model_md(2)),
        (last(), model_md(2)),
    ]
    assert output_text(run(script)[-1].response) == ["ab", "c"]


def test_tool_indexes_are_reused_across_turns():
    script = [
        (tool_start("ls", "call_1", block=0), model_md(1)),
        (last(), model_md(1)),
        (tool_result("call_1", content="one"), tools_md()),
        (tool_start("glob", "call_2", block=0), model_md(2)),
        (last(), model_md(2)),
        (tool_result("call_2", content="two"), tools_md()),
        (text("end", block=0), model_md(3)),
        (last(), model_md(3)),
    ]
    first, second, _ = run(script)[-1].response.output
    assert (first.name, first.output) == ("ls", "one")
    assert (second.name, second.output) == ("glob", "two")


def test_two_reasoning_blocks_in_one_turn_are_two_items():
    script = [
        (reasoning("one", block=0), model_md()),
        (reasoning("two", block=1), model_md()),
        (last(), model_md()),
    ]
    first, second = run(script)[-1].response.output
    assert first.summary[0].text == "one"
    assert second.summary[0].text == "two"


def test_usage_sums_every_call_and_maps_the_details():
    usage = run(TWO_TURNS)[-1].response.usage
    assert usage.input_tokens == 300
    assert usage.output_tokens == 40
    assert usage.total_tokens == 340
    assert usage.input_tokens_details.cached_tokens == 6
    assert usage.output_tokens_details.reasoning_tokens == 4


def test_usage_is_null_when_no_chunk_reports_it():
    script = [(text("hi"), model_md()), (last(usage=None), model_md())]
    assert run(script)[-1].response.usage is None


def test_response_fields_are_echoed():
    events = run(
        TWO_TURNS,
        response_id="resp_fixed",
        model="my-model",
        instructions="be brief",
        metadata={"k": "v"},
        previous_response_id="resp_prev",
        store=False,
    )
    for event in (events[0], events[1], events[-1]):
        dumped = event.response.model_dump()
        assert dumped["id"] == "resp_fixed"
        assert dumped["model"] == "my-model"
        assert dumped["instructions"] == "be brief"
        assert dumped["metadata"] == {"k": "v"}
        assert dumped["previous_response_id"] == "resp_prev"
        assert dumped["store"] is False
        assert dumped["tools"] == []
        assert dumped["tool_choice"] == "auto"
        assert dumped["parallel_tool_calls"] is True


def test_default_model_name():
    assert EventTranslator().start()[0].response.model == "steve-agent"


def test_no_model_turn_at_all_completes_with_empty_output():
    events = run([])
    assert events[-1].type == "response.completed"
    assert events[-1].response.output == []


def test_incomplete_last_turn():
    script = [
        (text("cut sh", block=0), model_md()),
        (last(status="incomplete", incomplete_details={"reason": "max_output_tokens"}), model_md()),
    ]
    events = run(script)
    terminal = events[-1]
    assert terminal.type == "response.incomplete"
    assert terminal.response.status == "incomplete"
    assert terminal.response.incomplete_details.reason == "max_output_tokens"
    assert terminal.response.error is None
    assert output_text(terminal.response) == ["cut sh"]
    assert terminal.response.output[0].status == "incomplete"


def test_incomplete_earlier_turn_does_not_decide_the_status():
    script = [
        (tool_start("ls", "call_1"), model_md(1)),
        (last(status="incomplete", incomplete_details={"reason": "max_output_tokens"}), model_md(1)),
        (tool_result("call_1"), tools_md()),
        (text("fine", block=0), model_md(2)),
        (last(), model_md(2)),
    ]
    terminal = run(script)[-1]
    assert terminal.type == "response.completed"
    assert terminal.response.incomplete_details is None


def test_unlisted_incomplete_reason_still_produces_a_terminal_event():
    script = [(text("x"), model_md()), (last(status="incomplete", incomplete_details={"reason": "new"}), model_md())]
    assert run(script)[-1].response.incomplete_details.reason == "new"


def test_truncated_last_turn_fails_with_partial_output():
    script = [
        (text("partial", block=0), model_md()),
        (last(status=None, usage=None), model_md()),
    ]
    terminal = run(script)[-1]
    assert terminal.type == "response.failed"
    assert terminal.response.status == "failed"
    assert terminal.response.error.code == "upstream_stream_truncated"
    assert output_text(terminal.response) == ["partial"]
    assert terminal.response.output[0].status == "incomplete"


def test_truncated_earlier_turn_does_not_decide_the_status():
    script = [
        (tool_start("ls", "call_1"), model_md(1)),
        (last(status=None, usage=None), model_md(1)),
        (tool_result("call_1"), tools_md()),
        (text("fine", block=0), model_md(2)),
        (last(), model_md(2)),
    ]
    assert run(script)[-1].type == "response.completed"


def test_truncated_last_turn_after_a_clean_turn_still_fails():
    script = [
        (tool_start("ls", "call_1"), model_md(1)),
        (last(), model_md(1)),
        (tool_result("call_1"), tools_md()),
        (text("partial", block=0), model_md(2)),
        (last(status=None, usage=None), model_md(2)),
    ]
    assert run(script)[-1].response.error.code == "upstream_stream_truncated"


def test_last_turn_with_no_last_chunk_counts_as_truncated_on_a_responses_upstream():
    assert run([(text("x"), model_md())])[-1].response.error.code == "upstream_stream_truncated"


def chat_finish(reason):
    return AIMessageChunk(content="", response_metadata={"finish_reason": reason}, chunk_position="last")


def test_a_chat_completions_turn_with_no_finish_reason_counts_as_truncated():
    script = [(AIMessageChunk(content="half"), model_md()), (last(status=None, usage=None), model_md())]
    terminal = run(script)[-1]
    assert terminal.response.error.code == "upstream_stream_truncated"
    assert terminal.response.output[0].status == "incomplete"


@pytest.mark.parametrize(
    "finish, reason", [("length", "max_output_tokens"), ("content_filter", "content_filter")]
)
def test_chat_completions_finish_reason_makes_the_response_incomplete(finish, reason):
    terminal = run([(AIMessageChunk(content="part"), model_md()), (chat_finish(finish), model_md())])[-1]
    assert terminal.type == "response.incomplete"
    assert terminal.response.incomplete_details.reason == reason
    assert terminal.response.output[0].status == "incomplete"


def test_only_the_last_chat_completions_turn_decides_the_status():
    script = [
        (AIMessageChunk(content="part"), model_md()),
        (chat_finish("length"), model_md()),
        (AIMessageChunk(content="whole"), model_md(2)),
        (chat_finish("stop"), model_md(2)),
    ]
    assert run(script)[-1].type == "response.completed"


def refusal(text):
    return AIMessageChunk(content=[{"type": "refusal", "refusal": text, "index": 0}])


def test_a_refusal_becomes_a_message_with_a_refusal_part():
    events = run([(refusal("I cannot."), model_md()), (last(), model_md())])
    item = events[-1].response.output[0]
    assert events[-1].type == "response.completed"
    assert (item.type, item.status) == ("message", "completed")
    assert [(part.type, part.refusal) for part in item.content] == [("refusal", "I cannot.")]
    assert [event.delta for event in events if event.type == "response.refusal.delta"] == ["I cannot."]
    assert [event.refusal for event in events if event.type == "response.refusal.done"] == ["I cannot."]


def test_completed_at_is_set_only_on_completed_responses():
    done = run([(text("ok"), model_md()), (last(), model_md())])[-1].response
    assert done.completed_at >= done.created_at
    assert run([(text("ok"), model_md())], end="fail")[-1].response.completed_at is None


OPEN_ITEMS = [
    (reasoning("thought", block=0), model_md()),
    (text("all done", block=1), model_md()),
    (last(), model_md()),
    (reasoning("second", block=0), model_md(2)),
    (text("half a sent", block=1), model_md(2)),
]


def test_failure_keeps_partial_output_and_closes_the_open_message():
    terminal = run(OPEN_ITEMS, end="fail")[-1]
    assert terminal.type == "response.failed"
    assert terminal.response.error.code == "server_error"
    assert terminal.response.error.message == "The agent run failed."
    statuses = [item.status for item in terminal.response.output]
    assert statuses == ["completed", "completed", "completed", "incomplete"]
    assert output_text(terminal.response) == ["all done", "half a sent"]


def test_failure_closes_open_reasoning_as_incomplete():
    terminal = run([(reasoning("half"), model_md())], end="fail")[-1]
    item = terminal.response.output[0]
    assert (item.type, item.status, item.summary[0].text) == ("reasoning", "incomplete", "half")


def test_failure_on_chat_completions_closes_the_message_as_incomplete():
    terminal = run([(AIMessageChunk(content="half"), model_md())], end="fail")[-1]
    assert terminal.response.output[0].status == "incomplete"


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param([(tool_start("ls", "call_1"), model_md()), (tool_args('{"a'), model_md())], id="arguments"),
        pytest.param([(tool_start("ls", "call_1"), model_md()), (last(), model_md())], id="running"),
    ],
)
@pytest.mark.parametrize("end", ["fail", "cancel"])
def test_open_tool_items_are_closed_as_incomplete(steps, end):
    events = run(steps, end=end)
    tool = events[-1].response.output[0]
    assert tool.status == "incomplete"
    assert tool.output is None and tool.error is None
    assert by_type(events, "mcp_call.failed") == []
    assert types(events)[-2:] == ["output_item.done", "failed"]
    assert by_type(events, "mcp_call_arguments.done")[0].arguments == tool.arguments


def test_run_that_ends_with_a_tool_still_open_is_closed_as_incomplete():
    script = [(tool_start("ls", "call_1"), model_md()), (last(), model_md())]
    terminal = run(script)[-1]
    assert terminal.type == "response.completed"
    assert terminal.response.output[0].status == "incomplete"


def test_cancel_keeps_partial_output_and_reports_cancelled():
    terminal = run(OPEN_ITEMS, end="cancel")[-1]
    assert terminal.type == "response.failed"
    assert terminal.response.status == "cancelled"
    assert terminal.response.error.code == "cancelled"
    assert output_text(terminal.response) == ["all done", "half a sent"]
    assert terminal.response.output[-1].status == "incomplete"


def test_failure_message_comes_from_the_caller():
    translator = EventTranslator()
    translator.start()
    terminal = translator.fail("safe message", code="rate_limit_exceeded")[-1]
    assert terminal.response.error.message == "safe message"
    assert terminal.response.error.code == "rate_limit_exceeded"


def test_no_function_or_custom_tool_items_in_any_event():
    serialized = "".join(event.model_dump_json() for event in run(TWO_TURNS))
    assert "function_call" not in serialized
    assert "custom_tool_call" not in serialized
