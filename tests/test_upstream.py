"""Upstreams that send no stream indexes, as llama.cpp behind a gateway does."""

import json
from collections import deque

from fake_upstream import Step

from steve_agent.upstream import _IndexFiller


def fill(*events):
    filler = _IndexFiller()
    raw = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
    # One byte at a time: an SSE line can be split across network chunks.
    out = b"".join(filler.feed(raw[i : i + 1]) for i in range(len(raw))) + filler.flush()
    return [json.loads(line[5:]) for line in out.splitlines() if line.startswith(b"data:")]


def test_filler_numbers_items_in_order_and_matches_late_done_events():
    a, b, a_text, b_delta, a_done, b_done = fill(
        {"type": "response.output_item.added", "item": {"id": "rs_1", "type": "reasoning"}},
        {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message"}},
        {"type": "response.reasoning_text.delta", "item_id": "rs_1", "delta": "x"},
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "y"},
        {"type": "response.output_item.done", "item": {"id": "rs_1", "type": "reasoning"}},
        {"type": "response.output_item.done", "item": {"id": "msg_1", "type": "message"}},
    )
    assert (a["output_index"], b["output_index"]) == (0, 1)
    assert (a_text["output_index"], b_delta["output_index"]) == (0, 1)
    assert (a_done["output_index"], b_done["output_index"]) == (0, 1)
    assert b_delta["content_index"] == 0


def test_filler_leaves_conforming_events_and_other_lines_alone():
    filler = _IndexFiller()
    event = {"type": "response.output_text.delta", "item_id": "m", "output_index": 4, "content_index": 2, "delta": "y"}
    raw = b"event: x\ndata: " + json.dumps(event).encode() + b"\n\ndata: [DONE]\n\n: ping\n\n"
    assert filler.feed(raw) + filler.flush() == raw


def test_final_messages_keep_text_and_arguments(server, upstream):
    """Without indexes LangChain merged everything onto one block, leaving the stored turn empty."""
    upstream.omit_stream_indexes = True
    upstream.steps.extend(
        [Step(reasoning=True, text="Looking.", tools=[("ls", '{"path": "/"}')]), Step(reasoning=True, text="First answer.")]
    )
    first = server.client.responses.create(model="steve-agent", input="hello")
    assert "First answer." in first.output_text

    upstream.steps.append(Step(text="Second answer."))
    server.client.responses.create(model="steve-agent", previous_response_id=first.id, input="again")

    replayed = upstream.requests[-1]["body"]["input"]
    calls = [i for i in replayed if i.get("type") == "function_call"]
    assert [json.loads(c["arguments"]) for c in calls] == [{"path": "/"}]
    assistant = json.dumps([i for i in replayed if i.get("role") == "assistant"])
    assert "Looking." in assistant and "First answer." in assistant


def test_subagent_result_reaches_the_parent(server, upstream):
    """The task tool reads the subagent's final message, which was empty without indexes."""
    upstream.omit_stream_indexes = True
    upstream.routes["ALPHA"] = deque([Step(reasoning=True, text="PONG from the subagent")])
    upstream.steps.extend(
        [
            Step(reasoning=True, tools=[("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))]),
            Step(reasoning=True, text="Parent done."),
        ]
    )
    response = server.client.responses.create(model="steve-agent", input="delegate")
    task = next(i for i in response.output if i.type == "mcp_call" and i.name == "task")
    assert "PONG from the subagent" in task.output
