"""Questions a run asks the person: approving a tool call, or answering an MCP server's elicitation.

The tool call waits while the question is open, so nothing is lost: an MCP server's session stays
alive, and the run carries on with the answer. A ContextVar carries this to wherever the question
comes from, because subagents and MCP callbacks run in tasks of their own, under this one.
"""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

Mode = Literal["manual", "auto", "planning"]
ANSWER_SECONDS = 15 * 60


class InteractionEvent(BaseModel):
    """A stream event of our own. The Responses API has none for a question asked during a run."""

    model_config = ConfigDict(extra="allow")
    type: str
    sequence_number: int = 0


class RunInteraction:
    def __init__(self, mode: Mode, publish: Callable[[str, dict[str, Any]], None], streaming: bool):
        self.mode = mode
        self.streaming = streaming
        self._publish = publish
        self._open: dict[str, asyncio.Future] = {}

    async def ask(self, kind: str, details: dict[str, Any]) -> Any | None:
        """Waits for the answer. None if nobody can answer, or nobody did in time."""
        if not self.streaming:
            return None
        question_id = f"ask_{uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._open[question_id] = future
        self._publish(f"steve.{kind}.requested", {"id": question_id, **details})
        try:
            return await asyncio.wait_for(future, ANSWER_SECONDS)
        except TimeoutError:
            return None
        finally:
            del self._open[question_id]
            self._publish("steve.interaction.resolved", {"id": question_id})

    def answer(self, question_id: str, answer: Any) -> bool:
        future = self._open.get(question_id)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True


current_interaction: ContextVar[RunInteraction | None] = ContextVar("steve_interaction", default=None)
