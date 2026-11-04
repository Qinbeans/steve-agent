import os
import socket
import sys
import threading
import time
from dataclasses import dataclass

import openai
import pytest
import uvicorn
from fake_upstream import FakeUpstream
from fastapi import FastAPI
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from steve_agent.models import SteveConfig


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.config/steve, the repo and LangSmith."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(sys, "argv", ["steve-agent"])
    for name in list(os.environ):
        if name.startswith(("LANGSMITH_", "LANGCHAIN_", "OPENAI_")):
            monkeypatch.delenv(name)


@pytest.fixture
def no_network(monkeypatch):
    """Fail the test on any connection attempt, even one the code under test swallows."""
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    yield
    assert attempts == []


class FakeChatModel(BaseChatModel):
    """Answers every call with the Responses API content shape the REPL prints."""

    reply: str = "pong"
    prompts: list = []

    @property
    def _llm_type(self):
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts.append(list(messages))
        message = AIMessage(content=[{"type": "text", "text": self.reply}])
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.fixture
def fake_model(monkeypatch):
    """Replace ChatOpenAI inside build_agent and return the fake that was injected."""
    model = FakeChatModel(prompts=[])
    monkeypatch.setattr("steve_agent.agent.ReplaySafeChatOpenAI", lambda **kwargs: model)
    return model


@dataclass
class LiveServer:
    url: str
    host_port: tuple[str, int]
    app: FastAPI
    agent: object
    config: SteveConfig
    client: openai.OpenAI
    uvicorn: uvicorn.Server


@pytest.fixture
def loopback_only(monkeypatch):
    """Fail any connection that would leave the machine, in tests that need real sockets."""
    real_connect = socket.socket.connect

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError(f"Test tried to reach {address}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def upstream():
    fake = FakeUpstream()
    yield fake
    fake.close()


@pytest.fixture
def steve_home(tmp_path):
    home = tmp_path / "steve_home"
    (home / "agents" / "memories").mkdir(parents=True)
    (home / "agents" / "skills").mkdir()
    (home / "agents" / "AGENTS.md").write_text("HOME_AGENTS_V1")
    (home / "agents" / "memories" / "MEMORY.md").write_text("MEMORY_V1")
    return home


@pytest.fixture
def start_server(upstream, steve_home, loopback_only):
    """Real app on real uvicorn on an ephemeral port, with a real agent pointing at the fake upstream."""
    from steve_agent.server.app import build_server_agent, create_app

    servers = []

    def start(*, heartbeat_seconds=15.0, use_responses_api=True, max_retries=0, **server_options):
        config = SteveConfig(
            model={
                "model": "fake-model",
                "base_url": upstream.base_url,
                "api_key": "sk-upstream-secret",
                "use_responses_api": use_responses_api,
            },
            server=server_options,
        )
        agent = build_server_agent(config, home=steve_home)
        app = create_app(agent, config, heartbeat_seconds=heartbeat_seconds)
        uvicorn_config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", timeout_graceful_shutdown=1
        )
        server = uvicorn.Server(uvicorn_config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append((server, thread))
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.01)
        host, port = server.servers[0].sockets[0].getsockname()[:2]
        url = f"http://{host}:{port}"
        client = openai.OpenAI(
            base_url=f"{url}/v1", api_key=server_options.get("api_key") or "unused", max_retries=max_retries
        )
        return LiveServer(url, (host, port), app, agent, config, client, server)

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "server did not shut down"


@pytest.fixture
def server(start_server):
    return start_server()
