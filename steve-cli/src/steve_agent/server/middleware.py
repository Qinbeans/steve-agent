import asyncio
import base64
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from uuid import uuid4

import openai
from deepagents.middleware._utils import append_to_system_message
from deepagents.middleware.filesystem import (
    _READ_FILE_MEDIA_RESULT,
    FilesystemMiddleware,
    _replace_rejected_file_content,
)
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.summarization import SummarizationToolMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelResponse
from langchain_core.exceptions import ModelInvalidRequestError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command
from pypdf import PdfReader

from steve_agent.interaction import current_interaction
from steve_agent.server.events import TASK_TOOL

class FreshMemoryMiddleware(MemoryMiddleware):
    """Reload memory on every run. Checkpoints cache memory_contents, which would go stale."""

    async def abefore_agent(self, state, runtime, config):
        state = {k: v for k, v in state.items() if k != "memory_contents"}
        return await super().abefore_agent(state, runtime, config)


MEDIA_CALL_SECONDS = 180


def _pdf_text(data: bytes, path: str) -> str:
    try:
        pages = [page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages]
    except Exception as error:  # pypdf raises many types on damaged or encrypted files
        return f"Could not read the PDF {path}: {error}"
    if not any(page.strip() for page in pages):
        return f"The PDF {path} has {len(pages)} pages but no text to extract. It may be scanned."
    return "\n\n".join(f"--- Page {number} ---\n{page}" for number, page in enumerate(pages, 1))


def _readable_read(result):
    """Turn a read_file media result into what the model can use.

    A PDF becomes its text, which works on any model and can be evicted and paged like other long
    results. An image moves to a user message after the tool result, because llama.cpp only accepts
    text in tool output. The library sends sampled video frames the same way.
    """
    if not isinstance(result, ToolMessage) or result.name != "read_file":
        return result
    blocks = [block for block in result.content_blocks if block["type"] in ("image", "file") and "base64" in block]
    if len(blocks) != 1:
        return result
    block = blocks[0]
    path = result.additional_kwargs.get("read_file_path", "the file")
    if block.get("mime_type") == "application/pdf":
        return ToolMessage(
            content=_pdf_text(base64.b64decode(block["base64"]), path),
            name="read_file",
            tool_call_id=result.tool_call_id,
            status="success",
        )
    if block["type"] != "image":
        return result
    tool_message = ToolMessage(
        content=f"Read image {path}. It is attached in the following message.",
        name="read_file",
        tool_call_id=result.tool_call_id,
        additional_kwargs={"read_file_path": path},
        status="success",
    )
    media_message = HumanMessage(
        id=str(uuid4()),
        content_blocks=[block],
        additional_kwargs={_READ_FILE_MEDIA_RESULT: True, "read_file_path": path, "read_file_tool_call_id": result.tool_call_id},
    )
    return Command(update={"messages": [tool_message, media_message]})


def _without_sdk_retries(request):
    """One attempt only: an image can crash a server short of GPU memory, so it should not be resent."""
    client = request.model.root_async_client.with_options(max_retries=0)
    return request.override(model=request.model.model_copy(update={"root_async_client": client, "async_client": client.chat.completions}))


_COMPACT_TOOL = "compact_conversation"
# Tools of ours that change nothing. `task` is here because a subagent's own tools are gated too.
_READ_ONLY_TOOLS = {"ls", "read_file", "glob", "grep", "write_todos", "task", _COMPACT_TOOL}
# These change nothing here, so planning can research with them. Manual still asks, because an address
# can carry what the agent has read to anywhere, and a page can ask it to.
_WEB_TOOLS = {"web_fetch", "web_crawl"}

_PLANNING_PROMPT = """## Planning mode

You are in planning mode, so nothing can be changed. Think the request through carefully, look at whatever you need with your read-only tools, and finish with a clear plan: the steps in order, the files involved, the decisions it depends on and the risks. Do not say you made changes. The user will switch modes to carry the plan out."""

_MANUAL_PROMPT = """## Manual mode

The user approves each tool call that changes something. If one is denied, do not try it again or work around it: say what you wanted to do and ask how to proceed."""


def _is_read_only(tool, name: str) -> bool:
    """An MCP server says so in its tool's annotations, and only servers the user added are here."""
    if name in _READ_ONLY_TOOLS:
        return True
    return bool(getattr(tool, "metadata", None) and tool.metadata.get("readOnlyHint"))


def _refusal(request, text: str) -> ToolMessage:
    call = request.tool_call
    return ToolMessage(content=text, name=call["name"], tool_call_id=call["id"], status="error")


async def _gate_tool(request) -> ToolMessage | None:
    """What the mode does about this tool call: a refusal, or None to go ahead."""
    interaction = current_interaction.get()
    name = request.tool_call["name"]
    if interaction is None or interaction.mode == "auto" or _is_read_only(request.tool, name):
        return None
    if interaction.mode == "planning" and name in _WEB_TOOLS:
        return None
    if interaction.mode == "planning":
        return _refusal(request, f"{name} is not available in planning mode, which changes nothing. Include this step in your plan.")
    answer = await interaction.ask("approval", {"tool": name, "arguments": request.tool_call.get("args") or {}})
    if answer is None:
        return _refusal(request, f"{name} needs the user's approval, and it did not come.")
    if not answer.get("approve"):
        reason = str(answer.get("reason") or "").strip()
        return _refusal(request, f"The user denied {name}." + (f" They said: {reason}" if reason else ""))
    return None


def _today() -> str:
    now = datetime.now().astimezone()
    return f"{now:%A, %B} {now.day}, {now:%Y}"  # Not %-d, which Windows does not have.


def _with_today(request):
    """Asked for on every call, so a server left running does not go on giving yesterday's date.

    Models otherwise spend their reasoning on how a date can be later than their training data.
    """
    prompt = (
        f"Today is {_today()}. This is later than your training data, which is normal. Take the date as "
        "correct without weighing it up, and treat what you read that is newer as current."
    )
    return request.override(system_message=append_to_system_message(request.system_message, prompt))


def _apply_mode(request):
    interaction = current_interaction.get()
    if interaction is None or interaction.mode == "auto":
        return request
    prompt = _PLANNING_PROMPT if interaction.mode == "planning" else _MANUAL_PROMPT
    changes = {"system_message": append_to_system_message(request.system_message, prompt)}
    if interaction.mode == "planning":
        # Not offered at all, as a model offered a tool will try it. _gate_tool still refuses them.
        changes["tools"] = [
            tool
            for tool in request.tools or []
            if isinstance(tool, dict) or _is_read_only(tool, tool.name) or tool.name in _WEB_TOOLS
        ]
    return request.override(**changes)


class ReadableFilesMiddleware(FilesystemMiddleware):
    """File tools that give the model PDFs as text and images in a form it accepts.

    Taking the library's name takes its slot in the main agent and the general-purpose subagent.
    A 5xx or no answer in time, on a request that carries a file just read, counts as a rejection
    of that file, so the library replaces it with a notice. Otherwise it would stay in the
    conversation and fail every later request too.
    """

    @property
    def name(self) -> str:
        return FilesystemMiddleware.__name__

    def wrap_tool_call(self, request, handler):
        return super().wrap_tool_call(request, lambda r: _readable_read(handler(r)))

    async def awrap_tool_call(self, request, handler):
        refusal = await _gate_tool(request)
        if refusal is not None:
            return refusal

        async def readable(r):
            return await asyncio.to_thread(_readable_read, await handler(r))

        return await super().awrap_tool_call(request, readable)

    def wrap_model_call(self, request, handler):
        def rejecting(r):
            try:
                return handler(r)
            except openai.InternalServerError as error:
                if _replace_rejected_file_content(r.messages) == r.messages:
                    raise
                raise ModelInvalidRequestError(str(error)) from error

        return super().wrap_model_call(_with_today(request), rejecting)

    async def awrap_model_call(self, request, handler):
        async def rejecting(r):
            if _replace_rejected_file_content(r.messages) == r.messages:
                return await handler(r)
            try:
                return await asyncio.wait_for(handler(_without_sdk_retries(r)), MEDIA_CALL_SECONDS)
            except (openai.InternalServerError, TimeoutError) as error:
                raise ModelInvalidRequestError(str(error) or "The model did not answer in time.") from error

        return await super().awrap_model_call(_with_today(_apply_mode(request)), rejecting)


class MemoryBudgetMiddleware(AgentMiddleware):
    """Tell the agent to prune a memory file that has grown past the limit.

    The size is read before every model call, so the instruction stops as soon as the agent has
    pruned the file, instead of lasting to the end of the run. The agent decides what to keep.
    """

    def __init__(self, backend, paths, max_chars):
        self._backend = backend
        self._paths = paths
        self._max_chars = max_chars

    def _instruction(self, responses) -> str | None:
        oversized = []
        for response in responses:
            if response.content is not None:
                size = len(response.content.decode(errors="replace"))
                if size > self._max_chars:
                    oversized.append(f"- {response.path}: {size} characters")
        if not oversized:
            return None
        target = self._max_chars * 3 // 4
        return (
            "## Memory maintenance\n\n"
            f"These memory files are over the {self._max_chars} character limit:\n" + "\n".join(oversized) + "\n\n"
            f"Before doing anything else, prune each one to under {target} characters. Read the file, then replace "
            "it with write_file. Merge duplicates and remove entries that are outdated, trivial or easy to find "
            "again. Keep anything the user explicitly asked you to remember, and do not mention this upkeep unless asked."
        )

    def _with_instruction(self, request, instruction):
        if not instruction:
            return request
        return request.override(system_message=append_to_system_message(request.system_message, instruction))

    def wrap_model_call(self, request, handler):
        instruction = self._instruction(self._backend.download_files(self._paths))
        return handler(self._with_instruction(request, instruction))

    async def awrap_model_call(self, request, handler):
        instruction = self._instruction(await self._backend.adownload_files(self._paths))
        return await handler(self._with_instruction(request, instruction))


@dataclass(frozen=True)
class RunContext:
    instructions: str | None = None
    on_task_start: Callable[[str, str], None] | None = None
    compact: bool = False  # this run does nothing but compact the conversation
    on_model_call: Callable[[dict[str, int], int | None], None] | None = None
    on_note: Callable[[str], None] | None = None


_forced_compaction: ContextVar[bool] = ContextVar("steve_forced_compaction", default=False)
_MANUAL_KEEP = 3  # the last exchange, and the tool call that a compaction run adds after it


class ManualCompactionMiddleware(SummarizationToolMiddleware):
    """The compact_conversation tool, which a compaction run calls by itself.

    The library's tool refuses below about 42% of the context, which is right for a model deciding
    on its own but not for someone asking to compact. A compaction run makes no model decision: it
    answers its own model calls, so the only upstream call is the one that writes the summary.

    It also keeps only the last exchange. The library keeps 10% of the context, so on a large
    context a request to compact would do nothing until the conversation was tens of thousands of
    tokens long. A model calling the tool for itself keeps the library's amount.
    """

    def __init__(self, summarization, *, system_prompt=None):
        super().__init__(summarization, system_prompt=system_prompt)
        default_cutoff = summarization._determine_cutoff_index

        def cutoff(messages):
            if _forced_compaction.get():
                return summarization._lc_helper._find_safe_cutoff(messages, _MANUAL_KEEP)
            return default_cutoff(messages)

        summarization._determine_cutoff_index = cutoff

    def _is_eligible_for_compaction(self, messages):
        return _forced_compaction.get() or super()._is_eligible_for_compaction(messages)

    async def _arun_compact(self, runtime):
        context = runtime.context
        token = _forced_compaction.set(bool(context and context.compact))
        try:
            return await super()._arun_compact(runtime)
        finally:
            _forced_compaction.reset(token)

    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        if not (context and context.compact):
            return await super().awrap_model_call(request, handler)
        last = request.messages[-1] if request.messages else None
        if isinstance(last, ToolMessage) and last.tool_call_id.startswith("compact_"):
            if context.on_note:
                context.on_note(last.text)
            return ModelResponse(result=[AIMessage(last.text)])
        call = {"name": _COMPACT_TOOL, "args": {}, "id": f"compact_{uuid4().hex}", "type": "tool_call"}
        return ModelResponse(result=[AIMessage("", tool_calls=[call])])


def _estimate(text: str) -> int:
    return len(text) // 4


def estimate_context(request) -> dict[str, int]:
    """Roughly what a model call's prompt is made of, in tokens."""
    parts = {"System": 0, "Tools": 0, "User": 0, "Assistant": 0, "Reasoning": 0, "Tool results": 0}
    if request.system_message:
        parts["System"] += count_tokens_approximately([request.system_message])
    for tool in request.tools or []:
        parts["Tools"] += _estimate(str(tool if isinstance(tool, dict) else convert_to_openai_tool(tool)))
    for message in request.messages:
        tokens = count_tokens_approximately([message])
        if isinstance(message, HumanMessage):
            parts["User"] += tokens
        elif isinstance(message, ToolMessage):
            parts["Tool results"] += tokens
        else:
            reasoning = 0
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, dict) and block.get("type") == "reasoning":
                        reasoning += sum(_estimate(part.get("text", "")) for part in block.get("summary") or [])
            parts["Reasoning"] += min(reasoning, tokens)
            parts["Assistant"] += max(tokens - reasoning, 0)
    return parts


class ContextMeterMiddleware(AgentMiddleware):
    """Report what each of the top-level agent's model calls sent."""

    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        if not (context and context.on_model_call):
            return await handler(request)
        parts = estimate_context(request)
        response = await handler(request)
        usage = response.result[0].usage_metadata if getattr(response, "result", None) else None
        context.on_model_call(parts, usage.get("input_tokens") if usage else None)
        return response


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
