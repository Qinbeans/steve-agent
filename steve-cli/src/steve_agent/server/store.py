"""Bounded map from response ids to checkpoints, working from memory and writing through to disk.

Single worker. Without a database, a restart loses everything. With one, a conversation is saved
when a run ends, and loaded back the first time it is asked for.
"""

import logging
import sqlite3

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
)
from openai.types.responses.response_error import ResponseError

from steve_agent.server.conversations import ConversationDb, ThreadRows
from steve_agent.server.errors import ApiError
from steve_agent.server.runner import RunChannel

logger = logging.getLogger(__name__)


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
    compaction: bool = False

    def bytes(self) -> int:
        events = sum(len(event.model_dump_json(warnings=False)) for event in self.channel.events)
        return events + sum(len(str(item)) for item in self.input_items)


@dataclass
class _Thread:
    responses: set[str] = field(default_factory=set)
    runs: int = 0
    over_cap: bool = False


def normalize_input_items(request_input: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The request's input in the shape GET input_items returns, every item with an id."""
    items = [{"role": "user", "content": request_input}] if isinstance(request_input, str) else request_input
    normalized = []
    seen = set()
    for item in items:
        item = {"type": "message", **item}
        # Uploaded files live in the conversation's state; a second copy here would only hold memory.
        if isinstance(item.get("content"), list):
            item["content"] = [
                {k: v for k, v in part.items() if k != "file_data"} if isinstance(part, dict) else part
                for part in item["content"]
            ]
        if item["type"] == "message" and isinstance(item.get("content"), str):
            part_type = "output_text" if item.get("role") == "assistant" else "input_text"
            item["content"] = [{"type": part_type, "text": item["content"]}]
        # input_items pages by id, so a repeated id gets a new one: its cursor would be ambiguous.
        if not isinstance(item.get("id"), str) or item["id"] in seen:
            item["id"] = f"{'msg' if item['type'] == 'message' else 'item'}_{uuid4().hex}"
        seen.add(item["id"])
        normalized.append(item)
    return normalized


def _dumped(response: Response) -> dict[str, Any]:
    return response.model_dump(mode="json", warnings=False)


def _title(input_items: list[dict[str, Any]]) -> str:
    """The first words sent, or the first file's name if a message was only files."""
    files = []
    for item in input_items:
        content = item.get("content")
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "input_text" and part.get("text", "").strip():
                return part["text"].strip()[:60]
            if isinstance(part, dict) and part.get("filename"):
                files.append(part["filename"])
    return files[0][:60] if files else "Conversation"


def _restored_response(data: dict[str, Any]) -> Response:
    # Our error codes are not in the SDK's Literal, as in events._error.
    data = dict(data)
    error = data.pop("error", None)
    response = Response.model_validate(data)
    if error:
        response.error = ResponseError.model_construct(**error)
    return response


def _restored_channel(response: Response) -> RunChannel:
    """A channel like a compacted one: the terminal event alone, which holds the whole response."""
    event_type = {"completed": ResponseCompletedEvent, "failed": ResponseFailedEvent, "incomplete": ResponseIncompleteEvent}[response.status]
    channel = RunChannel(lambda: response)
    channel.events = [event_type(type=f"response.{response.status}", response=response, sequence_number=0)]
    return channel


class ResponseStore:
    """LRU over threads, because forks of one conversation share its checkpoints.

    Mutations happen synchronously, so nothing can interleave on the event loop; only
    the checkpoint deletion awaits, and it runs after the thread is out of the maps.
    """

    def __init__(
        self,
        checkpointer: InMemorySaver,
        max_conversations: int,
        max_conversation_mb: float = 64,
        db: ConversationDb | None = None,
        workspace: str | None = None,
    ):
        self._checkpointer = checkpointer
        self._db = db
        self._workspace = workspace or ""
        self._max = max_conversations
        self._max_bytes = max_conversation_mb * 1024 * 1024
        self._threads: OrderedDict[str, _Thread] = OrderedDict()
        self._records: dict[str, ResponseRecord] = {}
        self._dead: list[str] = []
        # Responses of runs in flight. Children finished before their parent stay here until it ends.
        self._live: dict[str, RunChannel] = {}
        self._live_children: set[str] = set()
        self._live_input_items: dict[str, list[dict[str, Any]]] = {}

    def get(self, response_id: str) -> ResponseRecord | None:
        self._ensure_loaded(response_id)
        return self._records.get(response_id)

    def is_child(self, response_id: str) -> bool:
        self._ensure_loaded(response_id)
        record = self._records.get(response_id)
        return bool(record.child_of) if record else response_id in self._live_children

    def input_items(self, response_id: str) -> list[dict[str, Any]] | None:
        """The input of a response in flight or stored."""
        self._ensure_loaded(response_id)
        record = self._records.get(response_id)
        if record:
            return record.input_items
        return self._live_input_items.get(response_id)

    def channel(self, response_id: str) -> RunChannel | None:
        """The events of a response in flight or stored."""
        self._ensure_loaded(response_id)
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
            self._ensure_loaded(previous_response_id)
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
            if self._threads[parent.thread_id].over_cap:
                raise ApiError(
                    400,
                    "This conversation is too large to continue. Start a new one, or raise server.max_conversation_mb.",
                    param="previous_response_id",
                    code="conversation_too_large",
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
        if thread_id in self._threads and not thread.runs:
            self._prune(thread_id)
        if thread_id in self._threads and records:
            self._save(thread_id)
        self._evict(keep=thread_id)

    def delete(self, response_id: str) -> bool:
        self._ensure_loaded(response_id)
        record = self._records.pop(response_id, None)
        if record is None:
            return False
        thread = self._threads[record.thread_id]
        thread.responses.discard(response_id)
        for child_id in record.children:
            del self._records[child_id]
            thread.responses.discard(child_id)
        self._drop_if_empty(record.thread_id)
        if record.thread_id in self._threads:
            self._save(record.thread_id)
        return True

    def list_conversations(self, limit: int = 200) -> list[dict[str, Any]]:
        """The saved conversations of the current working folder, newest first."""
        return self._guarded(lambda: self._db.list_threads(self._workspace, limit), []) if self._db else []

    def conversation(self, thread_id: str) -> dict[str, Any] | None:
        """A conversation's current line, oldest first: forks left behind by a retry are not in it."""
        self._ensure_thread(thread_id)
        if thread_id not in self._threads:
            return None
        records = self._conversation(thread_id)
        by_id = {record.response.id: record for record in records}
        turns = []
        current = records[-1] if records else None
        while current:
            turns.append(current)
            current = by_id.get(current.parent_id)
        turns.reverse()
        return {
            "id": thread_id,
            "title": (self._db and self._guarded(lambda: self._db.title(thread_id), None)) or (_title(turns[0].input_items) if turns else "Conversation"),
            "turns": [
                {"response": _dumped(record.response), "input_items": record.input_items, "compaction": record.compaction}
                for record in turns
            ],
        }

    def delete_conversation(self, thread_id: str) -> bool:
        """Forgets a whole conversation, on disk too. Raises if it has a run in flight."""
        self._ensure_thread(thread_id)
        thread = self._threads.get(thread_id)
        if thread is None:
            return False
        if thread.runs:
            raise ApiError(400, "This conversation has a response still running.", code="response_in_progress")
        self._drop(thread_id, forget=True)
        return True

    def _guarded(self, action, fallback):
        """A database problem costs the saved copy, and never the response in hand."""
        try:
            return action()
        except sqlite3.Error:
            logger.exception("Could not use the conversation database")
            return fallback

    def _ensure_loaded(self, response_id: str) -> None:
        if self._db is None or response_id in self._records or response_id in self._live:
            return
        thread_id = self._guarded(lambda: self._db.thread_of(response_id), None)
        if thread_id:
            self._ensure_thread(thread_id)

    def _ensure_thread(self, thread_id: str) -> None:
        if self._db is None or thread_id in self._threads:
            return
        rows = self._guarded(lambda: self._db.load(thread_id), None) if self._guarded(lambda: self._db.has_thread(thread_id), False) else None
        if rows is None:
            return
        saver = self._checkpointer
        for namespace, checkpoint_id, checkpoint_type, checkpoint, metadata_type, metadata, parent_id in rows.checkpoints:
            saver.storage[thread_id][namespace][checkpoint_id] = ((checkpoint_type, checkpoint), (metadata_type, metadata), parent_id)
        for namespace, checkpoint_id, task_id, idx, channel, value_type, value, task_path in rows.writes:
            saver.writes[(thread_id, namespace, checkpoint_id)][(task_id, idx)] = (task_id, channel, (value_type, value), task_path)
        for namespace, channel, version, value_type, value in rows.blobs:
            saver.blobs[(thread_id, namespace, channel, version)] = (value_type, value)
        thread = _Thread()
        self._threads[thread_id] = thread
        for row in rows.responses:
            response = _restored_response(row["response"])
            self._records[row["response_id"]] = ResponseRecord(
                thread_id=thread_id,
                checkpoint_id=row["checkpoint_id"],
                response=response,
                input_items=row["input_items"],
                parent_id=row["parent_id"],
                channel=_restored_channel(response),
                child_of=row["child_of"],
                children=row["children"],
                compaction=row["compaction"],
            )
            thread.responses.add(row["response_id"])
        thread.over_cap = self._conversation_bytes(thread_id) > self._max_bytes
        self._evict(keep=thread_id)

    def _save(self, thread_id: str) -> None:
        if self._db is None:
            return
        records = [record for record in self._records.values() if record.thread_id == thread_id]
        conversation = [record for record in records if not record.child_of]
        rows = ThreadRows(
            [
                {
                    "response_id": record.response.id,
                    "checkpoint_id": record.checkpoint_id,
                    "parent_id": record.parent_id,
                    "child_of": record.child_of,
                    "children": record.children,
                    "compaction": record.compaction,
                    "input_items": record.input_items,
                    "response": _dumped(record.response),
                }
                for record in records
            ],
            *self._saved_checkpoints(thread_id),
        )
        title = _title(conversation[0].input_items) if conversation else "Conversation"
        head = conversation[-1].response.id if conversation else None
        self._guarded(lambda: self._db.save(thread_id, title, self._workspace, head, rows), None)

    def _live_checkpoints(self, thread_id: str) -> tuple[dict, set]:
        """Each response's ancestor chain, and the channel values its checkpoint reads."""
        saver = self._checkpointer
        keep = {record.checkpoint_id for record in self._conversation(thread_id)} - {None}
        root = saver.storage.get(thread_id, {}).get("", {})
        chain = {}
        for checkpoint_id in keep:
            while checkpoint_id in root and checkpoint_id not in chain:
                chain[checkpoint_id] = root[checkpoint_id]
                checkpoint_id = root[checkpoint_id][2]
        versions = set()
        for checkpoint_id in keep:
            if checkpoint_id in root:
                for channel, version in saver.serde.loads_typed(root[checkpoint_id][0])["channel_versions"].items():
                    versions.add((thread_id, "", channel, version))
        return chain, versions

    def _saved_checkpoints(self, thread_id: str) -> tuple[list[tuple], list[tuple], list[tuple]]:
        """Only what continuing the conversation needs: dead branches and finished subagents stay off disk."""
        saver = self._checkpointer
        chain, versions = self._live_checkpoints(thread_id)
        checkpoints = [
            ("", checkpoint_id, checkpoint[0], checkpoint[1], metadata[0], metadata[1], parent)
            for checkpoint_id, (checkpoint, metadata, parent) in chain.items()
        ]
        writes = [
            ("", key[2], task_id, idx, channel, value[0], value[1], task_path)
            for key, entries in saver.writes.items()
            if key[0] == thread_id and not key[1] and key[2] in chain
            for (task_id, idx), (_, channel, value, task_path) in entries.items()
        ]
        blobs = [("", key[2], str(key[3]), blob[0], blob[1]) for key, blob in saver.blobs.items() if key in versions]
        return checkpoints, writes, blobs

    async def collect(self) -> None:
        """Delete the checkpoints of threads that were dropped."""
        while self._dead:
            await self._checkpointer.adelete_thread(self._dead.pop())

    def _conversation(self, thread_id: str) -> list[ResponseRecord]:
        """The responses of a thread that can be continued, oldest first."""
        return [r for r in self._records.values() if r.thread_id == thread_id and not r.child_of]

    def _conversation_bytes(self, thread_id: str) -> int:
        records = sum(self._records[id].bytes() for id in self._threads[thread_id].responses)
        return records + self._checkpoint_bytes(thread_id)

    def _prune(self, thread_id: str) -> None:
        """Free what matters least first. Runs only with no run in flight, which may need any checkpoint."""
        thread = self._threads[thread_id]
        if self._conversation_bytes(thread_id) <= self._max_bytes:
            thread.over_cap = False
            return
        before = self._conversation_bytes(thread_id)

        self._prune_checkpoints(thread_id)
        for record in self._conversation(thread_id):
            if self._conversation_bytes(thread_id) <= self._max_bytes:
                break
            self._compact(record)
        for record in self._conversation(thread_id)[:-1]:
            if self._conversation_bytes(thread_id) <= self._max_bytes:
                break
            self.delete(record.response.id)
            self._prune_checkpoints(thread_id)

        after = self._conversation_bytes(thread_id)
        thread.over_cap = after > self._max_bytes
        logger.info("Pruned conversation %s from %d to %d bytes", thread_id, before, after)

    def _compact(self, record: ResponseRecord) -> None:
        record.channel.compact()
        for child_id in record.children:
            self._records[child_id].channel.compact()

    def _checkpoint_bytes(self, thread_id: str) -> int:
        saver = self._checkpointer
        total = 0
        for namespace in saver.storage.get(thread_id, {}).values():
            for checkpoint, metadata, _ in namespace.values():
                total += len(checkpoint[1]) + len(metadata[1])
        for key, writes in saver.writes.items():
            if key[0] == thread_id:
                total += sum(len(write[2][1]) for write in writes.values())
        for key, blob in saver.blobs.items():
            if key[0] == thread_id:
                total += len(blob[1])
        return total

    def _prune_checkpoints(self, thread_id: str) -> None:
        """Free dead branches, finished subagents' checkpoints and channel values nothing reads.

        The whole ancestor chain of each response stays: deepagents keeps messages in a DeltaChannel,
        which rebuilds them from the writes stored on ancestors.
        """
        saver = self._checkpointer
        chain, versions = self._live_checkpoints(thread_id)
        namespaces = saver.storage.get(thread_id, {})
        namespaces.clear()
        namespaces[""] = chain
        for key in [k for k in saver.writes if k[0] == thread_id and (k[1] or k[2] not in chain)]:
            del saver.writes[key]
        for key in [k for k in saver.blobs if k[0] == thread_id and k not in versions]:
            del saver.blobs[key]

    def _drop_if_empty(self, thread_id: str) -> None:
        thread = self._threads[thread_id]
        if not thread.responses and not thread.runs:
            self._drop(thread_id, forget=True)

    def _evict(self, keep: str) -> None:
        # A thread with a run in flight is never evicted, so the cap can be exceeded until that run ends.
        for thread_id in list(self._threads):
            if len(self._threads) <= self._max:
                return
            if thread_id != keep and not self._threads[thread_id].runs:
                self._drop(thread_id)

    def _drop(self, thread_id: str, forget: bool = False) -> None:
        """Out of memory, which is all eviction means when it is saved. Only `forget` removes it from disk."""
        for response_id in self._threads.pop(thread_id).responses:
            del self._records[response_id]
        self._dead.append(thread_id)
        if forget and self._db:
            self._guarded(lambda: self._db.delete(thread_id), None)
