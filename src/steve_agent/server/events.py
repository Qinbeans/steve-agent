"""Translate agent stream tuples into Responses API events. Pure: no I/O and no awaits."""

import json
import time
from collections import Counter
from typing import get_args
from uuid import uuid4

from langchain_core.messages import AIMessageChunk, ToolMessage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseError,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
    ResponseInProgressEvent,
    ResponseMcpCallArgumentsDeltaEvent,
    ResponseMcpCallArgumentsDoneEvent,
    ResponseMcpCallCompletedEvent,
    ResponseMcpCallFailedEvent,
    ResponseMcpCallInProgressEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseReasoningSummaryPartAddedEvent,
    ResponseReasoningSummaryPartDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningSummaryTextDoneEvent,
    ResponseRefusalDeltaEvent,
    ResponseRefusalDoneEvent,
    ResponseTextDeltaEvent,
    ResponseTextDoneEvent,
    ResponseUsage,
)
from openai.types.responses.mcp_tool_call_error import McpToolExecutionError
from openai.types.responses.response import IncompleteDetails
from openai.types.responses.response_output_item import McpCall
from openai.types.responses.response_reasoning_item import Summary

SERVER_LABEL = "steve"
TASK_TOOL = "task"
TRUNCATED = "upstream_stream_truncated"
# Chat-completions finish_reason values that mean the answer was cut short, as Responses incomplete reasons.
CHAT_INCOMPLETE_REASONS = {"length": "max_output_tokens", "content_filter": "content_filter"}


def _new_id(prefix):
    return f"{prefix}_{uuid4().hex}"


class EventTranslator:
    """Turns (ns, chunk, metadata) tuples into events. Every public method returns a list of events.

    `namespace` is the subgraph whose output this translator reports: () for the top-level agent,
    or one subagent's namespace. `on_task_call` maps a task tool_call_id to its child response id.
    """

    def __init__(
        self,
        *,
        response_id=None,
        model="steve-agent",
        instructions=None,
        metadata=None,
        previous_response_id=None,
        store=True,
        namespace=(),
        on_task_call=None,
    ):
        self.response_id = response_id or _new_id("resp")
        self._created_at = time.time()
        self._model = model
        self._instructions = instructions
        self._metadata = metadata
        self._previous_response_id = previous_response_id
        self._store = store
        self.namespace = namespace
        self._on_task_call = on_task_call
        self._final = None

        self._sequence = 0
        self._pending = []
        self._output = []  # SDK items by output_index, mutated until their output_item.done
        self._usage = Counter()

        self._turn_key = None
        self._turn_status = None
        self._turn_incomplete_details = None

        # Open items, as output_index values.
        self._message = None
        self._reasoning = None
        self._reasoning_block = None
        self._summary_part = None
        self._turn_tools = {}  # tool_call_chunk index -> output_index, arguments still streaming
        self._running_tools = {}  # tool_call_id -> output_index, waiting for a ToolMessage

    def start(self):
        self._emit(ResponseCreatedEvent, response=self._response("in_progress"))
        self._emit(ResponseInProgressEvent, response=self._response("in_progress"))
        return self._drain()

    def feed(self, ns, chunk, metadata):
        if isinstance(chunk, AIMessageChunk) and chunk.usage_metadata:
            self._add_usage(chunk.usage_metadata)
        if self._is_own_token(ns, chunk, metadata):
            self._model_chunk(chunk, (ns, metadata["langgraph_checkpoint_ns"]))
        elif isinstance(chunk, ToolMessage) and ns == self.namespace and metadata.get("langgraph_node") == "tools":
            self._tool_result(chunk)
        return self._drain()

    def snapshot(self):
        """The response as it stands now, or its final form once ended."""
        return self._final or self._response("in_progress")

    def finish(self):
        """End a run that returned normally. The status is judged on the last model turn only."""
        self._close_turn()
        if self._turn_status == "incomplete":
            reason = (self._turn_incomplete_details or {}).get("reason")
            # model_construct: upstreams may send reasons the SDK's Literal does not list yet.
            details = IncompleteDetails.model_construct(reason=reason)
            return self._end("incomplete", ResponseIncompleteEvent, incomplete_details=details)
        if self._turn_key is not None and self._turn_status is None:
            return self.fail("The upstream model stream ended before it completed.", code=TRUNCATED)
        return self._end("completed", ResponseCompletedEvent, completed_at=time.time())

    def fail(self, message="The agent run failed.", code="server_error"):
        """End a failed run. The caller supplies a safe message; exceptions can hold secrets."""
        return self._end("failed", ResponseFailedEvent, error=_error(code, message))

    def cancel(self):
        """End a cancelled run. The API has no cancelled event, so response.failed carries the status."""
        return self._end("cancelled", ResponseFailedEvent, error=_error("cancelled", "The run was cancelled."))

    def _is_own_token(self, ns, chunk, metadata):
        # Summarization shares node and checkpoint_ns with the real call; only lc_source tells them apart.
        return (
            isinstance(chunk, AIMessageChunk)
            and ns == self.namespace
            and metadata.get("langgraph_node") == "model"
            and not metadata.get("lc_source")
            and metadata.get("langgraph_checkpoint_ns", "").count("|") == len(self.namespace)
        )

    def _emit(self, event_class, **fields):
        event_type = get_args(event_class.model_fields["type"].annotation)[0]
        self._pending.append(event_class(type=event_type, sequence_number=self._sequence, **fields))
        self._sequence += 1

    def _drain(self):
        events, self._pending = self._pending, []
        return events

    def _add_item(self, item):
        self._output.append(item)
        index = len(self._output) - 1
        self._emit(ResponseOutputItemAddedEvent, output_index=index, item=self._snapshot(index))
        return index

    def _item_done(self, index):
        self._emit(ResponseOutputItemDoneEvent, output_index=index, item=self._snapshot(index))

    def _snapshot(self, index):
        # Events are kept and serialized later, so they must not share the items being mutated.
        return self._output[index].model_copy(deep=True)

    def _add_usage(self, usage):
        self._usage.update(
            input=usage.get("input_tokens", 0),
            output=usage.get("output_tokens", 0),
            total=usage.get("total_tokens", 0),
            cached=usage.get("input_token_details", {}).get("cache_read", 0),
            cache_write=usage.get("input_token_details", {}).get("cache_creation", 0),
            reasoning=usage.get("output_token_details", {}).get("reasoning", 0),
        )

    def _response(self, status, **fields):
        return Response(
            id=self.response_id,
            created_at=self._created_at,
            model=self._model,
            object="response",
            status=status,
            output=[self._snapshot(index) for index in range(len(self._output))],
            tools=[],
            tool_choice="auto",
            parallel_tool_calls=True,
            instructions=self._instructions,
            metadata=self._metadata,
            previous_response_id=self._previous_response_id,
            store=self._store,
            usage=self._response_usage(),
            **fields,
        )

    def _response_usage(self):
        if not self._usage:
            return None
        return ResponseUsage(
            input_tokens=self._usage["input"],
            input_tokens_details={
                "cached_tokens": self._usage["cached"],
                "cache_write_tokens": self._usage["cache_write"],
            },
            output_tokens=self._usage["output"],
            output_tokens_details={"reasoning_tokens": self._usage["reasoning"]},
            total_tokens=self._usage["total"],
        )

    def _end(self, status, event_class, **fields):
        if status != "completed":
            self._close_text_items("incomplete")
        self._close_turn()
        for index in self._running_tools.values():
            self._output[index].status = "incomplete"
            self._item_done(index)
        self._running_tools.clear()
        self._final = self._response(status, **fields)
        self._emit(event_class, response=self._final)
        return self._drain()

    def _model_chunk(self, chunk, key):
        if key != self._turn_key:
            self._close_turn()
            self._turn_key = key
            self._turn_status = None
            self._turn_incomplete_details = None
        metadata = chunk.response_metadata
        self._turn_status = metadata.get("status", self._turn_status)
        self._turn_incomplete_details = metadata.get("incomplete_details", self._turn_incomplete_details)
        # A chat-completions upstream has finish_reason instead of status.
        if finish_reason := metadata.get("finish_reason"):
            incomplete_reason = CHAT_INCOMPLETE_REASONS.get(finish_reason)
            self._turn_status = "incomplete" if incomplete_reason else "completed"
            self._turn_incomplete_details = {"reason": incomplete_reason}

        self._reasoning_deltas(chunk)
        self._refusals(chunk)
        if chunk.text:
            self._text_delta(chunk.text)
        for tool_chunk in chunk.tool_call_chunks:
            self._tool_delta(tool_chunk)
        if chunk.chunk_position == "last":
            self._close_turn()

    def _close_turn(self):
        cut_or_incomplete = self._turn_status in ("incomplete", None)
        self._close_text_items("incomplete" if cut_or_incomplete else "completed")
        for index in self._turn_tools.values():
            item = self._output[index]
            self._emit(
                ResponseMcpCallArgumentsDoneEvent, item_id=item.id, output_index=index, arguments=item.arguments
            )
            self._emit(ResponseMcpCallInProgressEvent, item_id=item.id, output_index=index)
        self._turn_tools.clear()

    def _close_text_items(self, status):
        self._close_reasoning(status)
        self._close_message(status)

    def _reasoning_deltas(self, chunk):
        if not isinstance(chunk.content, list):
            return
        for block in chunk.content:
            if not isinstance(block, dict) or block.get("type") != "reasoning":
                continue
            # Encrypted-only blocks have no summary text, so they produce nothing here.
            for part in block.get("summary", []):
                if part.get("text"):
                    self._summary_delta(block.get("index"), part.get("index"), part["text"])

    def _summary_delta(self, block, part, delta):
        if self._reasoning is None or block != self._reasoning_block:
            self._close_text_items("completed")
            self._reasoning = self._add_item(
                ResponseReasoningItem(id=_new_id("rs"), summary=[], type="reasoning", status="in_progress")
            )
            self._reasoning_block = block
        if self._summary_part != part or not self._output[self._reasoning].summary:
            self._close_summary_part()
            self._open_summary_part()
            self._summary_part = part
        item = self._output[self._reasoning]
        item.summary[-1].text += delta
        self._emit(
            ResponseReasoningSummaryTextDeltaEvent,
            item_id=item.id,
            output_index=self._reasoning,
            summary_index=len(item.summary) - 1,
            delta=delta,
        )

    def _refusals(self, chunk):
        if not isinstance(chunk.content, list):
            return
        for block in chunk.content:
            if isinstance(block, dict) and block.get("type") == "refusal" and block.get("refusal"):
                self._refusal(block["refusal"])

    def _refusal(self, text):
        # The upstream's refusal arrives whole, so its message opens and closes here.
        self._close_text_items("completed")
        index = self._add_item(
            ResponseOutputMessage(id=_new_id("msg"), content=[], role="assistant", status="in_progress", type="message")
        )
        item = self._output[index]
        where = {"item_id": item.id, "output_index": index, "content_index": 0}
        part = ResponseOutputRefusal(refusal=text, type="refusal")
        self._emit(ResponseContentPartAddedEvent, part=ResponseOutputRefusal(refusal="", type="refusal"), **where)
        self._emit(ResponseRefusalDeltaEvent, delta=text, **where)
        self._emit(ResponseRefusalDoneEvent, refusal=text, **where)
        self._emit(ResponseContentPartDoneEvent, part=part, **where)
        item.content.append(part.model_copy())
        item.status = "completed"
        self._item_done(index)

    def _open_summary_part(self):
        item = self._output[self._reasoning]
        item.summary.append(Summary(text="", type="summary_text"))
        self._emit(
            ResponseReasoningSummaryPartAddedEvent,
            item_id=item.id,
            output_index=self._reasoning,
            summary_index=len(item.summary) - 1,
            part={"text": "", "type": "summary_text"},
        )

    def _close_summary_part(self):
        if self._reasoning is None or not self._output[self._reasoning].summary:
            return
        item = self._output[self._reasoning]
        where = {"item_id": item.id, "output_index": self._reasoning, "summary_index": len(item.summary) - 1}
        text = item.summary[-1].text
        self._emit(ResponseReasoningSummaryTextDoneEvent, text=text, **where)
        self._emit(ResponseReasoningSummaryPartDoneEvent, part={"text": text, "type": "summary_text"}, **where)

    def _close_reasoning(self, status):
        if self._reasoning is None:
            return
        self._close_summary_part()
        self._output[self._reasoning].status = status
        self._item_done(self._reasoning)
        self._reasoning = None

    def _text_delta(self, delta):
        if self._message is None:
            self._close_reasoning("completed")
            self._open_message()
        item = self._output[self._message]
        item.content[0].text += delta
        self._emit(
            ResponseTextDeltaEvent,
            item_id=item.id,
            output_index=self._message,
            content_index=0,
            delta=delta,
            logprobs=[],
        )

    def _open_message(self):
        self._message = self._add_item(
            ResponseOutputMessage(
                id=_new_id("msg"), content=[], role="assistant", status="in_progress", type="message"
            )
        )
        item = self._output[self._message]
        item.content.append(ResponseOutputText(text="", annotations=[], logprobs=[], type="output_text"))
        self._emit(
            ResponseContentPartAddedEvent,
            item_id=item.id,
            output_index=self._message,
            content_index=0,
            part=item.content[0].model_copy(deep=True),
        )

    def _close_message(self, status):
        if self._message is None:
            return
        item = self._output[self._message]
        where = {"item_id": item.id, "output_index": self._message, "content_index": 0}
        self._emit(ResponseTextDoneEvent, text=item.content[0].text, logprobs=[], **where)
        self._emit(ResponseContentPartDoneEvent, part=item.content[0].model_copy(deep=True), **where)
        item.status = status
        self._item_done(self._message)
        self._message = None

    def _tool_delta(self, tool_chunk):
        block = tool_chunk["index"]
        if block not in self._turn_tools:
            self._close_text_items("completed")
            name = tool_chunk.get("name") or ""
            # McpCall allows extra fields and the SDK keeps them, so clients get this on output_item.added.
            child = {}
            if name == TASK_TOOL and self._on_task_call:
                child["child_response_id"] = self._on_task_call(tool_chunk["id"])
            index = self._add_item(
                McpCall(
                    id=_new_id("mcp"),
                    arguments="",
                    name=name,
                    server_label=SERVER_LABEL,
                    type="mcp_call",
                    status="in_progress",
                    **child,
                )
            )
            self._turn_tools[block] = index
            self._running_tools[tool_chunk.get("id")] = index
        delta = tool_chunk.get("args")
        if delta:
            index = self._turn_tools[block]
            item = self._output[index]
            item.arguments += delta
            self._emit(ResponseMcpCallArgumentsDeltaEvent, item_id=item.id, output_index=index, delta=delta)

    def _tool_result(self, message):
        self._close_turn()
        index = self._running_tools.pop(message.tool_call_id, None)
        if index is None:
            return
        item = self._output[index]
        content = message.content if isinstance(message.content, str) else json.dumps(message.content, default=str)
        if message.status == "error":
            item.status = "failed"
            item.error = McpToolExecutionError(
                type="mcp_tool_execution_error", content=[{"type": "text", "text": content}]
            )
            self._emit(ResponseMcpCallFailedEvent, item_id=item.id, output_index=index)
        else:
            item.status = "completed"
            item.output = content
            self._emit(ResponseMcpCallCompletedEvent, item_id=item.id, output_index=index)
        self._item_done(index)


def _error(code, message):
    # model_construct: our codes are not in the SDK's Literal, and clients accept unknown ones.
    return ResponseError.model_construct(code=code, message=message)
