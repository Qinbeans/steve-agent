import os
from collections.abc import Sequence
from pathlib import Path

import yaml
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from steve_agent.models.configs import SteveConfig
from steve_agent.server.middleware import FreshMemoryMiddleware
from steve_agent.upstream import http_clients

_SKILLS = ["/skills"]
_MEMORY = ["/memories/MEMORY.md", "/AGENTS.md"]


_INSTRUCTIONS = """
You are a self-improving research assistant.

Always read the AGENTS.md files to understand the current instructions and behavior modifications.

If /local path does not exist, default to the root directory.

You have access to various skills and memory resources to assist in research tasks.
Memories can be added to /memories or /local/memories. With writes prioritizing /local/memories unless otherwise specified.
To change behavior, change /local/AGENTS.md files unless otherwise specified.
Reading skills are prioritized with /skills first, followed by /local/skills. Writing skills should be saved to /local/skills unless otherwise specified.
"""


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


def get_local() -> Path | None:
    local_path = Path(".agents")
    if not local_path.exists():
        return None
    return local_path


class ReplaySafeChatOpenAI(ChatOpenAI):
    """Drops reasoning items that streaming left empty, because replaying them breaks later turns.

    LangChain's stream parser does not collect `reasoning_text`, which llama.cpp sends, so the
    aggregated item keeps `content: []`. llama.cpp answers that with a 400 ("item['content'] is not
    an array") and every later turn fails. These items hold nothing usable.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        items = payload.get("input")
        if isinstance(items, list):
            payload["input"] = [item for item in items if not _is_emptied_reasoning(item)]
        return payload


def _is_emptied_reasoning(item) -> bool:
    return (
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("content") == []
        and not item.get("summary")
        and not item.get("encrypted_content")
    )


def build_agent(
    config: SteveConfig,
    *,
    home: Path | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    middleware: Sequence[AgentMiddleware] = (),
    context_schema: type | None = None,
    fresh_memory: bool = False,
    conversation_artifacts: bool = False,
) -> CompiledStateGraph:
    home = home or default_home()

    local = get_local()

    skills = list(_SKILLS)
    memory = list(_MEMORY)

    if local is not None:
        if (local / "skills").exists():
            skills.append("/local/skills")
        if (local / "memories").exists():
            memory.append("/local/memories/MEMORY.md")
        if (local / "AGENTS.md").exists():
            memory.append("/local/AGENTS.md")

    agents_fs = FilesystemBackend(
        root_dir="./.agents",
        virtual_mode=True,
    )

    home_fs = FilesystemBackend(
        root_dir=str(home / "agents"),
        virtual_mode=True,
    )

    routes = {}
    if local is not None:
        routes["/local"] = agents_fs
    routes["/agents"] = agents_fs

    # In graph state, what the middleware offloads (oversized input, summarized history, large tool
    # results) branches and is deleted with its conversation, and other conversations cannot read it.
    artifacts = {}
    if conversation_artifacts:
        routes["/artifacts/"] = StateBackend()
        artifacts["artifacts_root"] = "/artifacts"

    composite_fs = CompositeBackend(
        default=home_fs,
        routes=routes,
        **artifacts,
    )

    model = ReplaySafeChatOpenAI(
        model=config.model.model,
        base_url=config.model.base_url,
        api_key=config.model.api_key,
        use_responses_api=config.model.use_responses_api,
        **http_clients(),
    )

    middleware = list(middleware)
    if fresh_memory:
        middleware.insert(0, FreshMemoryMiddleware(backend=composite_fs, sources=memory))
        memory = None

    agent = create_deep_agent(
        model=model,
        system_prompt="You are a research assistant.",
        skills=skills,
        memory=memory,
        backend=composite_fs,
        checkpointer=checkpointer,
        middleware=middleware,
        context_schema=context_schema,
    )
    return agent
