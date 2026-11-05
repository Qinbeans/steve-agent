"""Commands, against the real bubblewrap. These try to get out, so a pass means something."""

import asyncio
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fake_upstream import Step

from steve_agent.models.configs import SteveSandboxConfig
from steve_agent.sandbox import MAX_OUTPUT_BYTES, SandboxedFolderBackend, bwrap_works, can_run_commands

pytestmark = pytest.mark.skipif(not bwrap_works(), reason="bubblewrap cannot run here")


def execute(command):
    return ("execute", json.dumps({"command": command}))


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


def run(folder, command, **settings):
    backend = SandboxedFolderBackend(folder, SteveSandboxConfig(**settings))
    return asyncio.run(backend.aexecute(command))


def test_a_command_runs_in_the_folder_and_can_change_it(folder):
    result = run(folder, "pwd; echo made > out.txt; mkdir -p a/b; echo deep > a/b/c.txt")
    assert result.exit_code == 0
    assert result.output.strip() == str(folder.resolve())
    assert (folder / "out.txt").read_text() == "made\n"
    assert (folder / "a/b/c.txt").read_text() == "deep\n"


def test_the_exit_code_and_errors_come_back(folder):
    result = run(folder, "echo oops >&2; exit 7")
    assert result.exit_code == 7
    assert "oops" in result.output


def test_a_command_cannot_read_outside_the_folder(folder, tmp_path):
    (tmp_path / "secret.txt").write_text("OUTSIDE_SECRET")
    result = run(folder, f"cat {tmp_path}/secret.txt; ls {tmp_path}; cat ../secret.txt")
    assert "OUTSIDE_SECRET" not in result.output
    assert "secret.txt\n" not in result.output


def test_a_command_cannot_follow_a_link_out_of_the_folder(folder, tmp_path):
    (tmp_path / "secret.txt").write_text("OUTSIDE_SECRET")
    (folder / "link.txt").symlink_to(tmp_path / "secret.txt")
    result = run(folder, "cat link.txt; echo x > link.txt")
    assert "OUTSIDE_SECRET" not in result.output
    assert (tmp_path / "secret.txt").read_text() == "OUTSIDE_SECRET"


def test_a_command_cannot_write_outside_the_folder(folder, tmp_path):
    result = run(
        folder,
        f"echo x > {tmp_path}/escaped.txt; echo x > /usr/escaped.txt; echo x > /etc/escaped.txt; "
        "echo x > /escaped.txt; echo ran",
    )
    assert "ran" in result.output
    assert not (tmp_path / "escaped.txt").exists()
    assert not Path("/usr/escaped.txt").exists() and not Path("/etc/escaped.txt").exists()


def test_the_real_home_and_steve_config_are_not_there(folder):
    """On the real home of whoever runs this, not a stand-in."""
    home = Path.home()
    result = run(folder, f'ls -A {home}/.ssh {home}/.config/steve 2>&1; echo "HOME=$HOME"; ls -A "$HOME" | wc -l')
    assert "config.yaml" not in result.output
    assert "id_" not in result.output
    assert "HOME=/home/sandbox" in result.output
    assert result.output.strip().endswith("0"), "the home folder should be empty"


def test_the_environment_is_not_passed_on(folder, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_API_KEY", "LEAKED_VALUE")
    assert "LEAKED_VALUE" not in run(folder, "env; cat /proc/self/environ").output


def test_host_processes_cannot_be_seen(folder):
    result = run(folder, "ls /proc | grep -c '^[0-9]'")
    assert int(result.output.strip()) < 10, "only the sandbox's own processes should show"


@pytest.fixture
def listener():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        threading.Thread(target=lambda: server.accept(), daemon=True).start()
        yield server.getsockname()[1]


CONNECT = "python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 5); print('CONNECTED')\" 2>&1"


def test_no_network_by_default(folder, listener):
    # Not just "CONNECTED" missing from the output, as a traceback quotes the source that prints it.
    output = run(folder, CONNECT.format(port=listener)).output
    assert "Connection refused" in output and not output.strip().endswith("CONNECTED")


def test_the_internet_cannot_be_reached_by_default(folder):
    output = run(folder, "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 5)\" 2>&1").output
    assert "Network is unreachable" in output


def test_network_when_turned_on(folder, listener):
    assert run(folder, CONNECT.format(port=listener), network=True).output.strip().endswith("CONNECTED")


def alive(marker):
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0


def test_a_timeout_kills_everything_the_command_started(folder):
    marker = "86413"
    result = run(folder, f"(setsid sleep {marker} &) ; nohup sleep {marker} & sleep {marker}", timeout_seconds=1)
    assert result.exit_code == 124
    assert "stopped after 1 seconds" in result.output
    assert not alive(f"sleep {marker}")


def test_the_agent_cannot_ask_for_longer_than_the_setting(folder):
    backend = SandboxedFolderBackend(folder, SteveSandboxConfig(timeout_seconds=1))
    started = time.monotonic()
    result = asyncio.run(backend.aexecute("sleep 86414", timeout=3000))
    assert result.exit_code == 124 and time.monotonic() - started < 10


def test_stopping_a_run_kills_the_command(folder):
    marker = "86415"

    async def scenario():
        backend = SandboxedFolderBackend(folder, SteveSandboxConfig())
        task = asyncio.create_task(backend.aexecute(f"(setsid sleep {marker} &) ; sleep {marker}"))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if alive(f"sleep {marker}"):
                break
        assert alive(f"sleep {marker}")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not alive(f"sleep {marker}")


def test_background_work_ends_with_the_command(folder):
    marker = "86416"
    assert run(folder, f"sleep {marker} & echo started").exit_code == 0
    assert not alive(f"sleep {marker}")


def test_huge_output_keeps_the_start_and_end(folder):
    result = run(folder, "echo FIRST; head -c 5000000 /dev/zero | tr '\\0' x; echo; echo LAST")
    assert result.truncated
    assert len(result.output) < MAX_OUTPUT_BYTES + 200
    assert result.output.startswith("FIRST") and result.output.strip().endswith("LAST")


@pytest.mark.parametrize("pick", ["root", "home", "above home", "steve", "above steve", "missing"])
def test_folders_that_would_expose_keys_get_no_commands(pick, tmp_path, monkeypatch):
    person = tmp_path / "people/me"
    steve = tmp_path / "elsewhere/steve"
    person.mkdir(parents=True)
    steve.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: person)
    chosen = {
        "root": Path("/"),
        "home": person,
        "above home": person.parent,
        "steve": steve,
        "above steve": steve.parent,
        "missing": tmp_path / "nope",
    }[pick]
    assert not can_run_commands(chosen, steve)


def test_an_ordinary_folder_gets_commands(folder, tmp_path):
    assert can_run_commands(folder, tmp_path / "steve")


# Through the real agent


def test_the_agent_runs_a_command(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[execute("echo hi > made.txt; echo ALL_GOOD")]), Step(text="Done.")])
    response = server.client.responses.create(input="go", extra_body={"mode": "auto"})
    tool = next(item for item in response.output if item.type == "mcp_call")
    assert tool.status == "completed" and "ALL_GOOD" in tool.output
    assert (folder / "made.txt").read_text() == "hi\n"
    body = upstream.body_text()
    assert "You can run shell commands" in body and "no network" in body


def offered(upstream):
    return {tool.get("name") for tool in upstream.requests[0]["body"].get("tools", [])}


def test_no_command_tool_without_a_folder(start_server, upstream):
    server = start_server()
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="go")
    assert "execute" not in offered(upstream)


def test_no_command_tool_when_turned_off(start_server, upstream, folder):
    server = start_server(workspace=str(folder), sandbox={"enabled": False})
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="go")
    assert "execute" not in offered(upstream)
    assert "shell commands" not in upstream.body_text()


def test_no_command_tool_without_bubblewrap(start_server, upstream, folder, monkeypatch):
    """It must not fall back to running commands unsandboxed."""
    monkeypatch.setattr("steve_agent.sandbox.bwrap_works", lambda: False)
    server = start_server(workspace=str(folder))
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="go")
    assert "execute" not in offered(upstream)


def test_planning_does_not_offer_commands(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.append(Step(text="A plan."))
    server.client.responses.create(input="go", extra_body={"mode": "planning"})
    assert "execute" not in offered(upstream)


def test_the_prompt_says_when_there_is_network(start_server, upstream, folder):
    server = start_server(workspace=str(folder), sandbox={"network": True})
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="go")
    assert "can reach the network" in upstream.body_text()


def test_manual_asks_before_a_command(start_server, upstream, folder):
    from test_modes import Conversation

    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[execute("echo no > denied.txt")]), Step(text="Understood.")])

    def deny(event):
        return {"approve": False, "reason": "not now"}

    run_ = Conversation(server, deny, input="go", mode="manual")
    assert run_.questions[0]["tool"] == "execute"
    assert run_.questions[0]["arguments"]["command"] == "echo no > denied.txt"
    assert not (folder / "denied.txt").exists()
