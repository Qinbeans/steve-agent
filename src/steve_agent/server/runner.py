"""One background task drives the agent and publishes events; streaming and non-streaming read the same channel."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
from openai.types.responses import Response, ResponseStreamEvent

from steve_agent.server.events import EventTranslator
from steve_agent.server.middleware import RunContext

logger = logging.getLogger(__name__)

TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}


class RunChannel:
    """The events of one run, replayable from any sequence number."""

    def __init__(self, snapshot: Callable[[], Response] | None = None):
        self.events: list[ResponseStreamEvent] = []
        self.snapshot = snapshot
        self._changed = asyncio.Event()

    def publish(self, events: list[ResponseStreamEvent]) -> None:
        self.events.extend(events)
        self._changed.set()

    async def subscribe(
        self, starting_after: int = -1, idle_timeout: float | None = None
    ) -> AsyncIterator[ResponseStreamEvent | None]:
        """Yield events through the terminal one, and None each time idle_timeout passes with nothing new."""
        position = starting_after + 1
        while True:
            while position < len(self.events):
                event = self.events[position]
                position += 1
                yield event
                if event.type in TERMINAL_EVENTS:
                    return
            # A replay can start at or past the terminal event, and nothing more will ever arrive.
            if self.events and self.events[-1].type in TERMINAL_EVENTS:
                return
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), idle_timeout)
            except TimeoutError:
                yield None

    async def terminal(self) -> ResponseStreamEvent:
        async for event in self.subscribe():
            pass
        return event


class ChildRun:
    """One task call's own response: the subagent's events, translated apart from the parent's."""

    def __init__(self, translator: EventTranslator):
        self.translator = translator
        self.channel = RunChannel(translator.snapshot)
        self.ended = False
        self.channel.publish(translator.start())

    def feed(self, ns: tuple[str, ...], chunk: BaseMessage, metadata: dict) -> None:
        self.channel.publish(self.translator.feed(ns, chunk, metadata))

    def end(self, events: list[ResponseStreamEvent]) -> None:
        self.ended = True
        self.channel.publish(events)


class ChildRuns:
    """The task calls of one run, routed by subgraph namespace.

    Every call gets a child even when nobody watches, because a watcher can join at any time and replay.
    A call can reach open() and bind() in either order: the model's chunks wait in the stream queue
    while the tool node is already running. Its namespace is always bound before its first chunk is read.
    """

    def __init__(
        self,
        new_translator: Callable[[], EventTranslator] = EventTranslator,
        on_open: Callable[[ChildRun], None] | None = None,
    ):
        self._new_translator = new_translator
        self._on_open = on_open
        self._by_call: dict[str, ChildRun] = {}
        self._by_namespace: dict[str, ChildRun] = {}

    def __iter__(self):
        return iter(self._by_call.values())

    def open(self, tool_call_id: str) -> str:
        return self._child(tool_call_id).translator.response_id

    def bind(self, namespace: str, tool_call_id: str) -> None:
        child = self._child(tool_call_id)
        child.translator.namespace = (namespace,)
        self._by_namespace[namespace] = child

    def feed(self, ns: tuple[str, ...], chunk: BaseMessage, metadata: dict) -> None:
        if ns:
            if child := self._by_namespace.get(ns[0]):
                child.feed(ns, chunk, metadata)
        elif isinstance(chunk, ToolMessage) and (child := self._by_call.get(chunk.tool_call_id)) and not child.ended:
            translator = child.translator
            child.end(translator.fail("The subagent failed.") if chunk.status == "error" else translator.finish())

    def end_all(self, parent_status: str) -> None:
        """End children still open when the parent ends, so that their watchers are never left waiting."""
        for child in self:
            if not child.ended:
                translator = child.translator
                cancelled = parent_status == "cancelled"
                child.end(translator.cancel() if cancelled else translator.fail("The parent run ended first."))

    def _child(self, tool_call_id: str) -> ChildRun:
        if tool_call_id not in self._by_call:
            self._by_call[tool_call_id] = ChildRun(self._new_translator())
            if self._on_open:
                self._on_open(self._by_call[tool_call_id])
        return self._by_call[tool_call_id]


class Run:
    """A running agent turn. Cancelling the task stops the agent and the upstream model call."""

    def __init__(
        self,
        agent: CompiledStateGraph,
        translator: EventTranslator,
        messages: list[BaseMessage],
        instructions: str | None,
        thread_id: str,
        checkpoint_id: str | None = None,
        on_end: Callable[[Response, str | None, list[ChildRun]], None] | None = None,
        after_end: Callable[[], Awaitable[None]] | None = None,
        children: ChildRuns | None = None,
    ):
        self.channel = RunChannel(translator.snapshot)
        self.head: str | None = None
        self._agent = agent
        self._translator = translator
        self._messages = messages
        self._children = children or ChildRuns()
        self._context = RunContext(instructions=instructions, on_task_start=self._children.bind)
        self._configurable = {"thread_id": thread_id}
        if checkpoint_id:
            self._configurable["checkpoint_id"] = checkpoint_id
        self._on_end = on_end
        self._after_end = after_end
        self.task = asyncio.create_task(self._drive())

    async def _drive(self) -> None:
        self.channel.publish(self._translator.start())
        try:
            self._end(await self._consume())
        except asyncio.CancelledError:
            self._end(self._translator.cancel())
            raise
        except Exception:
            # Exceptions can hold secrets or URLs, so the client gets the translator's generic message.
            logger.exception("Agent run %s failed", self._translator.response_id)
            self._end(self._translator.fail())
        finally:
            if self._after_end:
                await self._after_end()

    async def _consume(self) -> list[ResponseStreamEvent]:
        # skills_metadata=None makes the agent reload skills; checkpoints would otherwise keep them stale.
        stream = self._agent.astream(
            {"messages": self._messages, "skills_metadata": None},
            {"configurable": self._configurable},
            context=self._context,
            subgraphs=True,
            stream_mode=["messages", "checkpoints"],
        )
        try:
            async for ns, mode, payload in stream:
                if mode == "messages":
                    self._children.feed(ns, *payload)
                    self.channel.publish(self._translator.feed(ns, *payload))
                elif not ns:
                    self.head = payload["config"]["configurable"]["checkpoint_id"]
        finally:
            await stream.aclose()
        return self._translator.finish()

    def _end(self, events: list[ResponseStreamEvent]) -> None:
        response = events[-1].response
        self._children.end_all(response.status)
        # Record before publishing: a client that sees the terminal event may continue from it at once.
        if self._on_end:
            self._on_end(response, self.head, list(self._children))
        self.channel.publish(events)
