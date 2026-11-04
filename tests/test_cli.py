import builtins
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
import yaml
from test_agent import MODEL_ONLY, write_home
from test_server import wait_until


@pytest.fixture
def spy_uvicorn(monkeypatch):
    started = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: started.append((app, options)))
    return started


def config_home(tmp_path, **server):
    home = tmp_path / "home" / ".config" / "steve"
    write_home(home)
    (home / "config.yaml").write_text(yaml.safe_dump(MODEL_ONLY | {"server": server}))
    return home


def run_cli(monkeypatch, *args):
    from steve_agent import main

    monkeypatch.setattr(sys, "argv", ["steve-agent", *args])
    return main()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "steve.example"])
def test_serve_refuses_non_loopback_without_api_key(tmp_path, monkeypatch, spy_uvicorn, host):
    config_home(tmp_path)
    monkeypatch.setattr(
        "steve_agent.server.app.build_server_agent",
        lambda *args, **kwargs: pytest.fail("the agent must not be built before the refusal"),
    )

    with pytest.raises(SystemExit) as caught:
        run_cli(monkeypatch, "serve", "--host", host)

    assert "Refusing to serve" in str(caught.value.code)
    assert host in str(caught.value.code)
    assert spy_uvicorn == []


def test_serve_refuses_non_loopback_host_from_config(tmp_path, monkeypatch, spy_uvicorn):
    config_home(tmp_path, host="0.0.0.0")

    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "serve")

    assert spy_uvicorn == []


def test_serve_allows_non_loopback_with_api_key(tmp_path, monkeypatch, spy_uvicorn, fake_model):
    config_home(tmp_path, api_key="s3cret")

    run_cli(monkeypatch, "serve", "--host", "0.0.0.0", "--port", "9001")

    _, options = spy_uvicorn[0]
    assert options["host"] == "0.0.0.0"
    assert options["port"] == 9001
    assert options["workers"] == 1


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_serve_allows_loopback_without_api_key(tmp_path, monkeypatch, spy_uvicorn, fake_model, host):
    config_home(tmp_path)

    run_cli(monkeypatch, "serve", "--host", host)

    assert spy_uvicorn[0][1]["host"] == host


def test_serve_defaults_come_from_the_config(tmp_path, monkeypatch, spy_uvicorn, fake_model):
    config_home(tmp_path, host="127.0.0.5", port=9100)

    run_cli(monkeypatch, "serve")

    assert spy_uvicorn[0][1]["host"] == "127.0.0.5"
    assert spy_uvicorn[0][1]["port"] == 9100


def test_serve_port_zero_is_not_replaced_by_the_default(tmp_path, monkeypatch, spy_uvicorn, fake_model):
    config_home(tmp_path)

    run_cli(monkeypatch, "serve", "--port", "0")

    assert spy_uvicorn[0][1]["port"] == 0


def test_serve_first_run_prompts_for_config(tmp_path, monkeypatch, spy_uvicorn, fake_model):
    answers = iter(["my-model", "http://127.0.0.1:9/v1", "my-key"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))

    run_cli(monkeypatch, "serve")

    assert (tmp_path / "home" / ".config" / "steve" / "config.yaml").is_file()
    assert len(spy_uvicorn) == 1


def test_repl_is_the_default_and_does_not_serve(tmp_path, monkeypatch, spy_uvicorn, fake_model, capsys):
    config_home(tmp_path)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")

    run_cli(monkeypatch)

    assert capsys.readouterr().out == "Welcome to Steve Agent!\n"
    assert spy_uvicorn == []


def test_unknown_command_is_an_error(monkeypatch):
    with pytest.raises(SystemExit) as caught:
        run_cli(monkeypatch, "nonsense")

    assert caught.value.code == 2


def real_command_env(tmp_path):
    return {"HOME": str(tmp_path / "home"), "PATH": os.environ["PATH"]}


def real_command():
    return str(Path(sys.executable).with_name("steve-agent"))


def test_real_command_refuses_non_loopback_without_api_key(tmp_path):
    config_home(tmp_path)

    result = subprocess.run(
        [real_command(), "serve", "--host", "0.0.0.0", "--port", "0"],
        env=real_command_env(tmp_path),
        cwd=tmp_path / "cwd",
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert "Refusing to serve" in result.stderr
    assert "Uvicorn running" not in result.stderr


def test_real_command_serves_and_shuts_down_cleanly(tmp_path, upstream):
    home = config_home(tmp_path)
    config = {"model": {"model": "m", "base_url": upstream.base_url, "api_key": "k"}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    process = subprocess.Popen(
        [real_command(), "serve", "--port", "0"],
        env=real_command_env(tmp_path),
        cwd=tmp_path / "cwd",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = []
    reader = threading.Thread(target=lambda: output.extend(process.stdout), daemon=True)
    reader.start()
    try:
        wait_until(lambda: any("Uvicorn running" in line for line in output) or process.poll() is not None, 60)
        assert process.poll() is None, "".join(output)
        url = re.search(r"http://127\.0\.0\.1:\d+", "".join(output)).group()

        assert httpx.get(f"{url}/health").json() == {"status": "ok"}
        reply = httpx.post(f"{url}/v1/responses", json={"input": "hello"}, timeout=30).json()
        assert reply["output"][0]["content"][0]["text"] == "echo: hello"
        assert "HOME_AGENTS_MARKER" in json.dumps(upstream.requests[0]["body"])

        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=30) == 0
    finally:
        process.kill()
        process.wait()
    reader.join(timeout=5)

    text = "".join(output)
    assert "Application shutdown complete" in text
    assert "Traceback" not in text
    assert sorted(path.name for path in (tmp_path / "cwd").iterdir()) == []
