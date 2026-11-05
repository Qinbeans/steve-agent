import builtins
import subprocess
import sys
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from steve_agent.agent import _file_backend, build_agent, default_home, get_config
from steve_agent.models import SteveConfig, SteveServerConfig

MODEL_ONLY = {"model": {"model": "m", "base_url": "http://127.0.0.1:9/v1", "api_key": "k"}}


def script_input(monkeypatch, *answers):
    answers = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))


def write_home(home: Path):
    (home / "agents" / "memories").mkdir(parents=True)
    (home / "agents" / "skills").mkdir()
    (home / "agents" / "AGENTS.md").write_text("HOME_AGENTS_MARKER")
    (home / "agents" / "memories" / "MEMORY.md").write_text("MEMORY_V1")


def test_config_without_server_key_loads(tmp_path):
    home = tmp_path / "injected"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump(MODEL_ONLY))

    config = get_config(home)

    assert config.model.model == "m"
    assert config.server == SteveServerConfig()
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8000
    assert config.server.api_key is None
    assert config.server.allowed_hosts == ["localhost", "127.0.0.1", "[::1]"]
    assert config.server.max_conversations == 256


def test_config_reads_server_key(tmp_path):
    home = tmp_path / "injected"
    home.mkdir()
    data = MODEL_ONLY | {"server": {"port": 9001, "api_key": "secret"}}
    (home / "config.yaml").write_text(yaml.safe_dump(data))

    config = get_config(home)

    assert config.server.port == 9001
    assert config.server.api_key == "secret"
    assert config.server.host == "127.0.0.1"


def test_default_home_follows_home_directory(tmp_path):
    assert default_home() == tmp_path / "home" / ".config" / "steve"


def test_build_agent_uses_real_chat_openai_without_network(tmp_path, no_network, monkeypatch):
    built = []

    def spy(**kwargs):
        built.append(ChatOpenAI(**kwargs))
        return built[-1]

    monkeypatch.setattr("steve_agent.agent.ReplaySafeChatOpenAI", spy)
    home = tmp_path / "injected"
    write_home(home)

    agent = build_agent(SteveConfig(**MODEL_ONLY), home=home)

    assert hasattr(agent, "astream")
    assert built[0].use_responses_api is True
    assert not (tmp_path / "home" / ".config").exists()


def test_build_agent_reads_memory_from_injected_home(tmp_path, fake_model, no_network):
    home = tmp_path / "injected"
    write_home(home)

    agent = build_agent(SteveConfig(**MODEL_ONLY), home=home)
    result = agent.invoke({"messages": [HumanMessage("ping")]})

    assert result["messages"][-1].content[-1]["text"] == "pong"
    system_prompt = str(fake_model.prompts[-1][0].content)
    assert "You are a research assistant." in system_prompt
    assert "HOME_AGENTS_MARKER" in system_prompt
    assert "MEMORY_V1" in system_prompt
    assert not (tmp_path / "home" / ".config").exists()


async def test_fresh_memory_sees_edit_on_next_turn(tmp_path, fake_model, no_network):
    home = tmp_path / "injected"
    write_home(home)
    thread = {"configurable": {"thread_id": "t"}}

    agent = build_agent(
        SteveConfig(**MODEL_ONLY), home=home, checkpointer=InMemorySaver(), fresh_memory=True
    )
    await agent.ainvoke({"messages": [HumanMessage("one")]}, thread)
    assert "MEMORY_V1" in str(fake_model.prompts[-1][0].content)

    (home / "agents" / "memories" / "MEMORY.md").write_text("MEMORY_V2")
    await agent.ainvoke({"messages": [HumanMessage("two")]}, thread)

    system_prompt = str(fake_model.prompts[-1][0].content)
    assert "MEMORY_V2" in system_prompt
    assert "MEMORY_V1" not in system_prompt


async def test_without_fresh_memory_the_checkpoint_keeps_old_memory(tmp_path, fake_model, no_network):
    """The control for the test above: shows the middleware is what makes the edit visible."""
    home = tmp_path / "injected"
    write_home(home)
    thread = {"configurable": {"thread_id": "t"}}

    agent = build_agent(SteveConfig(**MODEL_ONLY), home=home, checkpointer=InMemorySaver())
    await agent.ainvoke({"messages": [HumanMessage("one")]}, thread)
    (home / "agents" / "memories" / "MEMORY.md").write_text("MEMORY_V2")
    await agent.ainvoke({"messages": [HumanMessage("two")]}, thread)

    assert "MEMORY_V1" in str(fake_model.prompts[-1][0].content)


def test_package_import_has_no_side_effects(tmp_path):
    code = (
        "import sys, steve_agent\n"
        "assert callable(steve_agent.main)\n"
        "assert steve_agent.__all__ == ['main']\n"
        "heavy = {'deepagents', 'langchain_openai', 'fastapi', 'steve_agent.__main__'}\n"
        "assert not heavy & set(sys.modules), heavy & set(sys.modules)\n"
    )
    home, cwd = tmp_path / "home", tmp_path / "cwd"

    subprocess.run(
        [sys.executable, "-c", code], check=True, cwd=cwd, env={"HOME": str(home), "PATH": ""}
    )

    assert list(home.iterdir()) == []
    assert list(cwd.iterdir()) == []


def test_repl_with_existing_config(tmp_path, fake_model, no_network, monkeypatch, capsys):
    from steve_agent import main

    home = tmp_path / "home" / ".config" / "steve"
    write_home(home)
    (home / "config.yaml").write_text(yaml.safe_dump(MODEL_ONLY))
    script_input(monkeypatch, "ping", "/quit")

    main()

    assert capsys.readouterr().out == "Welcome to Steve Agent!\npong\n"
    assert len(fake_model.prompts) == 1


def test_repl_empty_line_exits(tmp_path, fake_model, no_network, monkeypatch, capsys):
    from steve_agent import main

    home = tmp_path / "home" / ".config" / "steve"
    write_home(home)
    (home / "config.yaml").write_text(yaml.safe_dump(MODEL_ONLY))
    script_input(monkeypatch, "")

    main()

    assert capsys.readouterr().out == "Welcome to Steve Agent!\n"
    assert fake_model.prompts == []


def test_repl_first_run_prompts_and_writes_config(tmp_path, fake_model, no_network, monkeypatch, capsys):
    from steve_agent import main

    script_input(monkeypatch, "my-model", "http://127.0.0.1:9/v1", "my-key", "ping", "/quit")

    main()

    assert capsys.readouterr().out == "Welcome to Steve Agent!\npong\n"
    home = tmp_path / "home" / ".config" / "steve"
    assert yaml.safe_load((home / "config.yaml").read_text()) == {
        "model": {
            "model": "my-model",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "my-key",
            "use_responses_api": True,
        }
    }
    assert (home / "agents" / "AGENTS.md").is_file()
    assert (home / "agents" / "memories" / "MEMORY.md").is_file()
    assert (home / "agents" / "skills").is_dir()


def test_local_routes_to_the_agents_directory(tmp_path):
    """The memory that build_agent loads from .agents is the same file under each of its names."""
    home = tmp_path / "injected"
    write_home(home)
    workspace = tmp_path / "project"
    (workspace / ".agents").mkdir(parents=True)
    (workspace / ".agents/AGENTS.md").write_text("project instructions")

    backend = _file_backend(home, workspace, False)

    names = ["/.agents/AGENTS.md", "/local/AGENTS.md", "/agents/AGENTS.md"]
    assert [found.content for found in backend.download_files(names)] == [b"project instructions"] * 3
