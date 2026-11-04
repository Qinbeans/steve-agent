"""Bounded map from response ids to checkpoints. Restart loses everything: single worker, in memory."""

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from openai.types.responses import Response

from steve_agent.server.errors import ApiError
from steve_agent.server.runner import RunChannel


@dataclass
class ResponseRecord:
    thread_id: str
    checkpoint_id: str | None
    response: Response
    input_items: list[dict[str, Any]]
    parent_id: str | None
    channel: RunChannel
    child_of: str | None = None  # the response whose task call produced this one
    children: list[str] = field(default_factory=list)


@dataclass
class _Thread:
    responses: set[str] = field(default_factory=set)
    runs: int = 0


def normalize_input_items(request_input: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The request's input in the shape GET input_items returns, every item with an id."""
    items = [{"role": "user", "content": request_input}] if isinstance(request_input, str) else request_input
    normalized = []
    seen = set()
    for item in items:
        item = {"type": "message", **item}
        if item["type"] == "message" and isinstance(item.get("content"), str):
            part_type = "output_text" if item.get("role") == "assistant" else "input_text"
            item["content"] = [{"type": part_type, "text": item["content"]}]
        # input_items pages by id, so a repeated id gets a new one: its cursor would be ambiguous.
        if not isinstance(item.get("id"), str) or item["id"] in seen:
            item["id"] = f"{'msg' if item['type'] == 'message' else 'item'}_{uuid4().hex}"
        seen.add(item["id"])
        normalized.append(item)
    return normalized


class ResponseStore:
    """LRU over threads, because forks of one conversation share its checkpoints.

    Mutations happen synchronously, so nothing can interleave on the event loop; only
    the checkpoint deletion awaits, and it runs after the thread is out of the maps.
    """

    def __init__(self, checkpointer: BaseCheckpointSaver, max_conversations: int):
        self._checkpointer = checkpointer
        self._max = max_conversations
        self._threads: OrderedDict[str, _Thread] = OrderedDict()
        self._records: dict[str, ResponseRecord] = {}
        self._dead: list[str] = []
        # Responses of runs in flight. Children finished before their parent stay here until it ends.
        self._live: dict[str, RunChannel] = {}
        self._live_children: set[str] = set()
        self._live_input_items: dict[str, list[dict[str, Any]]] = {}

    def get(self, response_id: str) -> ResponseRecord | None:
        return self._records.get(response_id)

    def is_child(self, response_id: str) -> bool:
        record = self._records.get(response_id)
        return bool(record.child_of) if record else response_id in self._live_children

    def input_items(self, response_id: str) -> list[dict[str, Any]] | None:
        """The input of a response in flight or stored."""
        record = self._records.get(response_id)
        if record:
            return record.input_items
        return self._live_input_items.get(response_id)

    def channel(self, response_id: str) -> RunChannel | None:
        """The events of a response in flight or stored."""
        record = self._records.get(response_id)
        return record.channel if record else self._live.get(response_id)

    def track(
        self, response_id: str, channel: RunChannel, *, child: bool = False, input_items: list[dict[str, Any]] | None = None
    ) -> None:
        self._live[response_id] = channel
        self._live_input_items[response_id] = input_items or []
        if child:
            self._live_children.add(response_id)

    def untrack(self, response_ids: list[str]) -> None:
        for response_id in response_ids:
            self._live.pop(response_id, None)
            self._live_input_items.pop(response_id, None)
            self._live_children.discard(response_id)

    def begin_run(self, previous_response_id: str | None) -> tuple[str, str | None]:
        """Resolve where a run starts and pin its thread against eviction. Returns (thread_id, checkpoint_id)."""
        if previous_response_id is None:
            thread_id, checkpoint_id = uuid4().hex, None
            self._threads[thread_id] = _Thread()
        else:
            parent = self._records.get(previous_response_id)
            is_child = previous_response_id in self._live_children
            if parent is None and not is_child:
                raise ApiError(
                    404,
                    f"Previous response '{previous_response_id}' was not found.",
                    param="previous_response_id",
                    code="previous_response_not_found",
                )
            # A stored child has no checkpoint either, so this also makes children read-only.
            if is_child or parent.checkpoint_id is None:
                raise ApiError(
                    400,
                    f"Previous response '{previous_response_id}' cannot be continued.",
                    param="previous_response_id",
                    code="previous_response_not_continuable",
                )
            thread_id, checkpoint_id = parent.thread_id, parent.checkpoint_id
            self._threads.move_to_end(thread_id)
        self._threads[thread_id].runs += 1
        return thread_id, checkpoint_id

    def end_run(self, thread_id: str, *records: ResponseRecord) -> None:
        """Release a thread pinned by begin_run, recording the responses there are to keep."""
        thread = self._threads[thread_id]
        thread.runs -= 1
        self._threads.move_to_end(thread_id)
        for record in records:
            self._records[record.response.id] = record
            thread.responses.add(record.response.id)
        self._drop_if_empty(thread_id)
        self._evict(keep=thread_id)

    def delete(self, response_id: str) -> bool:
        record = self._records.pop(response_id, None)
        if record is None:
            return False
        thread = self._threads[record.thread_id]
        thread.responses.discard(response_id)
        for child_id in record.children:
            del self._records[child_id]
            thread.responses.discard(child_id)
        self._drop_if_empty(record.thread_id)
        return True

    async def collect(self) -> None:
        """Delete the checkpoints of threads that were dropped."""
        while self._dead:
            await self._checkpointer.adelete_thread(self._dead.pop())

    def _drop_if_empty(self, thread_id: str) -> None:
        thread = self._threads[thread_id]
        if not thread.responses and not thread.runs:
            self._drop(thread_id)

    def _evict(self, keep: str) -> None:
        # A thread with a run in flight is never evicted, so the cap can be exceeded until that run ends.
        for thread_id in list(self._threads):
            if len(self._threads) <= self._max:
                return
            if thread_id != keep and not self._threads[thread_id].runs:
                self._drop(thread_id)

    def _drop(self, thread_id: str) -> None:
        for response_id in self._threads.pop(thread_id).responses:
            del self._records[response_id]
        self._dead.append(thread_id)
