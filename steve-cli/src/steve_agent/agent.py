import os
from collections.abc import Sequence
from pathlib import Path

import yaml
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.middleware.summarization import create_summarization_middleware
from langchain.agents.middleware import AgentMiddleware
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from steve_agent.models.configs import SteveConfig, SteveSandboxConfig
from steve_agent.sandbox import SandboxedFolderBackend, can_run_commands
from steve_agent.server.middleware import (
    FreshMemoryMiddleware,
    ManualCompactionMiddleware,
    MemoryBudgetMiddleware,
    ReadableFilesMiddleware,
)
from steve_agent.upstream import http_clients
from steve_agent.web import make_web_tools

# With a working folder, / is that folder, so Steve's own files move here to stay out of the way of a
# project's. Without one, / is Steve's own files.
_OWN_ROUTE = "/.steve"


_INSTRUCTIONS = """
You are a self-improving research assistant.

Always read the AGENTS.md files to understand the current instructions and behavior modifications.

If /local path does not exist, default to the root directory.

You have access to various skills and memory resources to assist in research tasks.
Memories can be added to /memories or /local/memories. With writes prioritizing /local/memories unless otherwise specified.
To change behavior, change /local/AGENTS.md files unless otherwise specified.
Reading skills are prioritized with /skills first, followed by /local/skills. Writing skills should be saved to /local/skills unless otherwise specified.
"""


_COMPACT_PROMPT = (
    "If this conversation has grown long and older details no longer matter, call compact_conversation "
    "to summarize them and free up context. It refuses when there is too little to compact."
)


def default_home() -> Path:
    return Path.home() / ".config" / "steve"


def create_agent(home: Path):
    config_file = home / "config.yaml"
    agents_path = home / "agents"
    agents_file = agents_path / "AGENTS.md"
    skills_path = agents_path / "skills"
    memories_path = agents_path / "memories"
    memory_file = memories_path / "MEMORY.md"

    config_file.exists() or home.mkdir(parents=True, exist_ok=True, mode=0o700)
    model = input("Enter the model name: ")
    base_url = input("Enter the base URL: ")
    api_key = input("Enter the API key: ")
    # The file holds API keys, so it is private whatever the umask.
    with os.fdopen(os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        yaml.safe_dump(
            {
                "model": {
                    "model": model,
                    "base_url": base_url,
                    "api_key": api_key,
                    "use_responses_api": True,
                }
            },
            f,
        )
    agents_path.exists() or agents_path.mkdir(parents=True, exist_ok=True)
    agents_file.exists() or agents_file.touch()
    skills_path.exists() or skills_path.mkdir(parents=True, exist_ok=True)
    memories_path.exists() or memories_path.mkdir(parents=True, exist_ok=True)
    memory_file.exists() or memory_file.touch()


def get_config(home: Path | None = None) -> SteveConfig:
    home = home or default_home()
    config_file = home / "config.yaml"
    if not config_file.exists():
        create_agent(home)
    with open(config_file, "r") as f:
        config_data = yaml.safe_load(f)
    return SteveConfig(**config_data)


class ReplaySafeChatOpenAI(ChatOpenAI):
    """Puts streamed reasoning back in the shape llama.cpp requires before it is replayed.

    llama.cpp answers a reasoning item with no text in `content` with a 400 ("item['content'] is not
    an array"), which would fail every later turn. Streaming leaves the text in `summary` and
    `content: []`: see upstream.py. An item with nothing to replay is dropped.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        items = payload.get("input")
        if isinstance(items, list):
            payload["input"] = [fixed for item in items if (fixed := _replayable_reasoning(item)) is not None]
        return payload


def _replayable_reasoning(item):
    """The item to send: unchanged unless it is a reasoning item that streaming left with `content: []`."""
    if not (isinstance(item, dict) and item.get("type") == "reasoning" and item.get("content") == []):
        return item
    if item.get("encrypted_content"):
        return item
    text = "\n\n".join(part["text"] for part in item.get("summary") or [] if part.get("text"))
    if not text:
        return None
    return {**item, "summary": [], "content": [{"type": "reasoning_text", "text": text}]}


class WorkingFolderBackend(CompositeBackend):
    """Makes the working folder the root, with other names for it that work but do not show up.

    Each alias is a route, and CompositeBackend lists and searches every route at the root. So a search
    of / would return the same file once per name, and Steve's memory and skills alongside the project.
    """

    def __init__(self, default, routes, aliases, *, artifacts_root="/"):
        super().__init__(default, {**routes, **aliases}, artifacts_root=artifacts_root)
        self._aliases = tuple(prefix.rstrip("/") for prefix in aliases)
        self._visible = CompositeBackend(default, routes, artifacts_root=artifacts_root)

    def _names_alias(self, pattern: str) -> bool:
        return pattern.startswith(self._aliases)

    def ls(self, path):
        return self._visible.ls(path) if path == "/" else super().ls(path)

    async def als(self, path):
        return await self._visible.als(path) if path == "/" else await super().als(path)

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        target = self._visible if path in (None, "/") else super()
        return target.grep(pattern, path, glob, max_count=max_count)

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        target = self._visible if path in (None, "/") else super()
        return await target.agrep(pattern, path, glob, max_count=max_count)

    def glob(self, pattern, path=None):
        target = self._visible if path in (None, "/") and not self._names_alias(pattern) else super()
        return target.glob(pattern, path)

    async def aglob(self, pattern, path=None):
        target = self._visible if path in (None, "/") and not self._names_alias(pattern) else super()
        return await target.aglob(pattern, path)


def _file_backend(
    home: Path, workspace: Path | None, conversation_artifacts: bool, sandbox: SteveSandboxConfig | None = None
) -> CompositeBackend:
    def folder(path: Path) -> FilesystemBackend:
        return FilesystemBackend(root_dir=str(path), virtual_mode=True)

    own_files = home / "agents"
    # In graph state, what the middleware offloads (oversized input, summarized history, large tool
    # results) branches and is deleted with its conversation, and other conversations cannot read it.
    routes = {}
    artifacts = {}
    if conversation_artifacts:
        routes["/artifacts/"] = StateBackend()
        artifacts["artifacts_root"] = "/artifacts"

    if workspace is None:
        return CompositeBackend(default=folder(own_files), routes=routes, **artifacts)

    root = SandboxedFolderBackend(workspace, sandbox) if sandbox else folder(workspace)
    project = folder(workspace / ".agents")
    # Older conversations and the agent's habits use these. A real folder of the same name wins, so that
    # writing /skills/x.py in a project with a skills folder does not land in Steve's own skills.
    shorthand = {
        "workspace": root,
        "agents": project,
        "local": project,
        "memories": folder(own_files / "memories"),
        "skills": folder(own_files / "skills"),
    }
    aliases = {f"/{name}/": backend for name, backend in shorthand.items() if not (workspace / name).exists()}
    aliases[f"{_OWN_ROUTE}/"] = folder(own_files)
    # The agent is told the real path and people paste it. It reaches nothing that / does not.
    aliases[f"{workspace.resolve()}/"] = root
    return WorkingFolderBackend(root, routes, aliases, **artifacts)


def build_agent(
    config: SteveConfig,
    *,
    home: Path | None = None,
    workspace: Path | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    middleware: Sequence[AgentMiddleware] = (),
    context_schema: type | None = None,
    fresh_memory: bool = False,
    conversation_artifacts: bool = False,
    tools: Sequence = (),
) -> CompiledStateGraph:
    home = home or default_home()

    own = _OWN_ROUTE if workspace else ""
    skills = [f"{own}/skills"]
    memory = [f"{own}/memories/MEMORY.md", f"{own}/AGENTS.md"]

    local = workspace / ".agents" if workspace else None
    if local is not None and local.exists():
        if (local / "skills").exists():
            skills.append("/.agents/skills")
        if (local / "memories").exists():
            memory.append("/.agents/memories/MEMORY.md")
        if (local / "AGENTS.md").exists():
            memory.append("/.agents/AGENTS.md")

    system_prompt = "You are a research assistant."
    if workspace:
        system_prompt = (
            "You are an assistant for research and for working with files.\n\n"
            f"You are working in the folder {workspace}, and / is that folder: /notes.md is {workspace}/notes.md. "
            "The full path works as well. You can read, create and edit files here, and that is where they stay. "
            "Files outside it are out of reach, so do not look for them. Do not use your tools to explore unless "
            f"the request needs it. Your own memory and skills are under {_OWN_ROUTE}, separate from the project."
        )
        if conversation_artifacts:
            system_prompt += " /artifacts is this conversation's own storage, not a folder in the project."
            if (workspace / "artifacts").exists():
                system_prompt += f" The project's artifacts folder is at {workspace}/artifacts."

    sandbox = config.sandbox if workspace and config.sandbox.enabled and can_run_commands(workspace, home) else None
    if sandbox:
        reach = (
            "They can reach the network, and this machine's own services."
            if sandbox.network
            else "They have no network, so installing packages or downloading will fail."
        )
        system_prompt += (
            f"\n\nYou can run shell commands with execute. They start in {workspace}, in a sandbox where only that "
            "folder can be changed and the home folder is empty, so use relative paths or the full path: /notes.md "
            f"in a command is not your /notes.md. {reach} They stop after {sandbox.timeout_seconds} seconds."
        )

    composite_fs = _file_backend(home, workspace, conversation_artifacts, sandbox)

    if config.web.enabled:
        # Without a conversation to keep them with, crawls go with Steve's own files, out of the project.
        crawl_root = "/artifacts/crawl" if conversation_artifacts else f"{own}/crawl"
        tools = [*tools, *make_web_tools(config.web, composite_fs, crawl_root)]
        system_prompt += (
            "\n\nYou can read the web with web_fetch for one page and web_crawl for many. "
            + (
                "web_crawl can search with a query."
                if config.web.search_url
                else "You have no search engine, so start from addresses you know or were given."
            )
        )

    model = ReplaySafeChatOpenAI(
        model=config.model.model,
        base_url=config.model.base_url,
        api_key=config.model.api_key,
        use_responses_api=config.model.use_responses_api,
        **http_clients(),
    )
    # Without a profile, automatic summarization only starts at 170k tokens.
    if config.model.max_input_tokens:
        model.profile = {**(model.profile or {}), "max_input_tokens": config.model.max_input_tokens}

    # The caller's middleware goes first so that it wraps the rest, including a compaction run's own answers.
    middleware = [
        *middleware,
        ReadableFilesMiddleware(backend=composite_fs),
        MemoryBudgetMiddleware(
            backend=composite_fs,
            paths=[path for path in memory if path.endswith("MEMORY.md")],
            max_chars=config.memory.max_file_chars,
        ),
        ManualCompactionMiddleware(create_summarization_middleware(model, composite_fs), system_prompt=_COMPACT_PROMPT),
    ]
    if fresh_memory:
        middleware.insert(0, FreshMemoryMiddleware(backend=composite_fs, sources=memory))
        memory = None

    agent = create_deep_agent(
        model=model,
        tools=list(tools),
        system_prompt=system_prompt,
        skills=skills,
        memory=memory,
        backend=composite_fs,
        checkpointer=checkpointer,
        middleware=middleware,
        context_schema=context_schema,
    )
    return agent
