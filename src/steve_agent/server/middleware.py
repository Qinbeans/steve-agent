from collections.abc import Callable
from dataclasses import dataclass

from deepagents.middleware._utils import append_to_system_message
from deepagents.middleware.memory import MemoryMiddleware
from langchain.agents.middleware import AgentMiddleware

from steve_agent.server.events import TASK_TOOL

class FreshMemoryMiddleware(MemoryMiddleware):
    """Reload memory on every run. Checkpoints cache memory_contents, which would go stale."""

    async def abefore_agent(self, state, runtime, config):
        state = {k: v for k, v in state.items() if k != "memory_contents"}
        return await super().abefore_agent(state, runtime, config)


@dataclass(frozen=True)
class RunContext:
    instructions: str | None = None
    on_task_start: Callable[[str, str], None] | None = None


class InstructionsMiddleware(AgentMiddleware):
    """Add per-request instructions to the system prompt. They never enter state, so no later turn sees them."""

    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        if context and context.instructions:
            system_message = append_to_system_message(request.system_message, context.instructions)
            request = request.override(system_message=system_message)
        return await handler(request)


class TaskNamespaceMiddleware(AgentMiddleware):
    """Report which subgraph namespace each task call runs in, before its subagent starts.

    Parallel calls start in no fixed order and each gets its own `tools:<uuid>` namespace, and this
    is the one place where that namespace and the tool_call_id are both known. Custom middleware
    only wraps the top-level agent's tools, so subagents' own tool calls do not arrive here.
    """

    async def awrap_tool_call(self, request, handler):
        context = request.runtime.context
        if request.tool_call["name"] == TASK_TOOL and context and context.on_task_start:
            context.on_task_start(request.runtime.config["configurable"]["checkpoint_ns"], request.tool_call["id"])
        return await handler(request)
