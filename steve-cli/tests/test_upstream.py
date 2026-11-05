"""Upstreams that send no stream indexes, as llama.cpp behind a gateway does."""

import json
from collections import deque

from fake_upstream import Step

from steve_agent.upstream import _StreamAdjuster


def fill(*events):
    filler = _StreamAdjuster()
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
    filler = _StreamAdjuster()
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


def test_raw_reasoning_becomes_summary_deltas():
    filler = _StreamAdjuster()
    event = {"type": "response.reasoning_text.delta", "item_id": "rs", "delta": "x"}
    raw = b"event: response.reasoning_text.delta\ndata: " + json.dumps(event).encode() + b"\n\n"
    out = (filler.feed(raw) + filler.flush()).decode()
    assert "event: response.reasoning_summary_text.delta\n" in out
    data = json.loads(out.split("data: ")[1])
    assert data["type"] == "response.reasoning_summary_text.delta" and data["summary_index"] == 0


def test_reasoning_reaches_the_client_when_streaming_and_not(server, upstream):
    upstream.llama_like = True
    upstream.steps.append(Step(reasoning=True, text="Answer."))
    plain = server.client.responses.create(model="steve-agent", input="hello")
    assert [item.type for item in plain.output] == ["reasoning", "message"]
    assert "Thinking part A." in plain.output[0].summary[0].text

    upstream.steps.append(Step(reasoning=True, text="Answer."))
    with server.client.responses.stream(model="steve-agent", input="hello") as stream:
        deltas = [e.delta for e in stream if e.type == "response.reasoning_summary_text.delta"]
        final = stream.get_final_response()
    assert "Thinking part A." in "".join(deltas)
    assert final.output[0].status == "completed"


def test_reasoning_is_replayed_in_the_shape_llama_requires(server, upstream):
    upstream.llama_like = True
    upstream.steps.extend([Step(reasoning=True, text="Looking.", tools=[("ls", '{"path": "/"}')]), Step(reasoning=True, text="First.")])
    first = server.client.responses.create(model="steve-agent", input="hello")
    assert first.status == "completed"

    upstream.steps.append(Step(reasoning=True, text="Second."))
    second = server.client.responses.create(model="steve-agent", input="again", previous_response_id=first.id)
    assert second.status == "completed"
    replayed = [i for i in upstream.requests[-1]["body"]["input"] if i.get("type") == "reasoning"]
    assert replayed, "reasoning should be replayed, not lost"
    for item in replayed:
        assert item["content"][0]["type"] == "reasoning_text"
        assert "Thinking part A." in item["content"][0]["text"]
        assert item["summary"] == []


def payload_for(block):
    from langchain_core.messages import AIMessage, HumanMessage

    from steve_agent.agent import ReplaySafeChatOpenAI

    model = ReplaySafeChatOpenAI(model="m", base_url="http://127.0.0.1:9/v1", api_key="k", use_responses_api=True)
    history = [HumanMessage("one"), AIMessage(content=[block, {"type": "text", "text": "reply"}]), HumanMessage("two")]
    return [i for i in model._get_request_payload(history)["input"] if i.get("type") == "reasoning"]


def test_reasoning_with_nothing_to_replay_is_dropped():
    assert payload_for({"type": "reasoning", "id": "rs", "summary": [], "content": [], "status": "in_progress"}) == []


def test_other_upstreams_reasoning_is_left_alone():
    """Real OpenAI items have no `content: []`, and an encrypted item must go back as it came."""
    for block in (
        {"type": "reasoning", "id": "rs", "summary": []},
        {"type": "reasoning", "id": "rs", "summary": [{"type": "summary_text", "text": "s"}]},
        {"type": "reasoning", "id": "rs", "summary": [], "content": [{"type": "reasoning_text", "text": "t"}]},
        {"type": "reasoning", "id": "rs", "summary": [], "content": [], "encrypted_content": "e"},
    ):
        item = payload_for(block)[0]
        assert item["summary"] == block["summary"] and item.get("content") == block.get("content"), block
