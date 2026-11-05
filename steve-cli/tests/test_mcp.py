"""MCP servers, against a real server over stdio and HTTP, through the real agent."""

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fake_upstream import Step

from steve_agent.mcp import load_mcp, login, token_file

FAKE = str(Path(__file__).with_name("fake_mcp.py"))
STDIO = {"command": sys.executable, "args": [FAKE, "stdio"]}


def write_servers(home, **servers):
    (home / "mcp.json").write_text(json.dumps({"mcpServers": servers}))


def statuses_of(home):
    return {entry["name"]: entry for entry in load_mcp(home).servers}


def statuses(server):
    return {entry["name"]: entry for entry in httpx.get(f"{server.url}/v1/mcp").json()["servers"]}


def start_fake(mode, *args, script=FAKE):
    process = subprocess.Popen([sys.executable, script, mode, *args], stdout=subprocess.PIPE, text=True)
    port = json.loads(process.stdout.readline())["port"]
    deadline = time.monotonic() + 15
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return process, f"http://127.0.0.1:{port}/mcp"
        except OSError:
            assert time.monotonic() < deadline, "the fake MCP server did not start"
            time.sleep(0.05)


@pytest.fixture
def http_server():
    processes = []

    def start(mode, *args, script=FAKE):
        process, url = start_fake(mode, *args, script=script)
        processes.append(process)
        return url

    yield start
    for process in processes:
        process.terminate()
        process.wait(10)


def call(upstream, server, tool, arguments="{}", answer="Done."):
    upstream.steps.extend([Step(text="", tools=[(tool, arguments)]), Step(text=answer)])
    response = server.client.responses.create(input="go")
    return next(item for item in response.output if item.type == "mcp_call" and item.name == tool), response


def test_no_file_means_no_servers(start_server):
    assert statuses(start_server()) == {}


def test_a_stdio_servers_tools_reach_the_model_and_run(start_server, upstream, steve_home):
    write_servers(steve_home, fake=STDIO)
    server = start_server()
    assert statuses(server)["fake"]["status"] == "connected"
    assert set(statuses(server)["fake"]["tools"]) == {"fake_shout", "fake_explode"}

    tool, response = call(upstream, server, "fake_shout", '{"text": "hello"}')
    assert "fake_shout" in json.dumps(upstream.requests[0]["body"]["tools"])
    assert tool.status == "completed" and "SHOUTED HELLO" in tool.output
    assert "SHOUTED HELLO" in upstream.body_text()
    assert response.status == "completed"


def test_a_subagent_gets_them_too(start_server, upstream, steve_home):
    from collections import deque

    write_servers(steve_home, fake=STDIO)
    server = start_server()
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.routes["ALPHA"] = deque([Step(text="", tools=[("fake_shout", '{"text": "sub"}')]), Step(text="Shouted.")])
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="Done.")])
    server.client.responses.create(input="go")
    assert any("SHOUTED SUB" in json.dumps(request["body"]) for request in upstream.requests)


def test_a_tool_error_goes_to_the_agent(start_server, upstream, steve_home):
    write_servers(steve_home, fake=STDIO)
    server = start_server()
    tool, response = call(upstream, server, "fake_explode")
    assert "kaboom" in json.dumps(upstream.requests[-1]["body"])
    assert response.status == "completed"
    assert tool.output is None or "kaboom" in tool.output or tool.error


def test_a_bad_server_is_reported_and_the_rest_still_work(start_server, upstream, steve_home):
    write_servers(
        steve_home,
        missing={"command": "/no/such/command"},
        empty={},
        **{"bad name": STDIO},
        fake=STDIO,
    )
    server = start_server()
    found = statuses(server)
    assert found["missing"]["status"] == "failed" and found["missing"]["error"]
    assert "command or a url" in found["empty"]["error"]
    assert "name can only" in found["bad name"]["error"]
    tool, _ = call(upstream, server, "fake_shout", '{"text": "still"}')
    assert "SHOUTED STILL" in tool.output


def test_a_broken_file_is_reported(start_server, steve_home):
    (steve_home / "mcp.json").write_text("{not json")
    assert "Could not read" in statuses(start_server())["mcp.json"]["error"]


def test_status_holds_no_secrets(start_server, steve_home, http_server):
    write_servers(steve_home, remote={"url": http_server("http", "s3cret"), "headers": {"Authorization": "Bearer wrong-s3cret"}})
    assert "s3cret" not in json.dumps(statuses(start_server()))


def test_http_with_a_bearer_token(start_server, upstream, steve_home, http_server):
    url = http_server("http", "s3cret")
    write_servers(steve_home, remote={"url": url, "headers": {"Authorization": "Bearer s3cret"}}, refused={"url": url})
    server = start_server()
    found = statuses(server)
    assert found["remote"]["status"] == "connected"
    assert found["refused"]["status"] == "failed" and "401" in found["refused"]["error"]
    tool, _ = call(upstream, server, "remote_shout", '{"text": "token"}')
    assert "SHOUTED TOKEN" in tool.output


def test_a_server_that_goes_away_during_a_call_does_not_end_the_run(start_server, upstream, steve_home):
    url_process, url = start_fake("http")
    write_servers(steve_home, remote={"url": url})
    server = start_server()
    url_process.terminate()
    url_process.wait(10)
    _, response = call(upstream, server, "remote_shout", '{"text": "gone"}')
    assert response.status == "completed"
    assert "MCP server failed" in upstream.body_text()


# OAuth


def browser(url):
    """Stands in for the person: the fake server approves at once, and the redirect lands on our callback."""
    assert httpx.get(url, follow_redirects=True, timeout=10).status_code == 200


def test_oauth_before_signing_in(start_server, steve_home, http_server):
    write_servers(steve_home, remote={"url": http_server("oauth"), "auth": "oauth"})
    found = statuses(start_server())["remote"]
    assert found["status"] == "needs_login" and found["tools"] == []


def test_oauth_sign_in_then_tools_work(start_server, upstream, steve_home, http_server):
    write_servers(steve_home, remote={"url": http_server("oauth"), "auth": "oauth"})
    tools = asyncio.run(login(steve_home, "remote", browser))
    assert "remote_shout" in tools

    saved = token_file(steve_home, "remote")
    assert json.loads(saved.read_text())["tokens"]["access_token"] == "issued-1"
    assert saved.stat().st_mode & 0o077 == 0, "tokens should be private"

    server = start_server()
    found = statuses(server)["remote"]
    assert found["status"] == "connected"
    assert "issued-1" not in json.dumps(found)
    tool, _ = call(upstream, server, "remote_shout", '{"text": "oauth"}')
    assert "SHOUTED OAUTH" in tool.output


def test_oauth_refused_in_the_browser_is_reported(steve_home, http_server):
    write_servers(steve_home, remote={"url": http_server("oauth"), "auth": "oauth"})

    def deny(url):
        assert httpx.get(url + "&deny=1", follow_redirects=True, timeout=10).status_code == 200

    with pytest.raises(ValueError, match="access_denied"):
        asyncio.run(login(steve_home, "remote", deny))


def test_login_needs_an_oauth_server(steve_home):
    write_servers(steve_home, fake=STDIO)
    with pytest.raises(ValueError, match="not a server"):
        asyncio.run(login(steve_home, "fake", browser))
    with pytest.raises(ValueError, match="not a server"):
        asyncio.run(login(steve_home, "absent", browser))


def test_load_mcp_needs_no_running_loop(steve_home):
    write_servers(steve_home, fake=STDIO)
    assert load_mcp(steve_home).servers[0]["status"] == "connected"


# Servers built with the fastmcp package

FASTMCP = str(Path(__file__).with_name("fake_fastmcp.py"))


def test_fastmcp_over_stdio(start_server, upstream, steve_home):
    write_servers(steve_home, fm={"command": sys.executable, "args": [FASTMCP, "stdio"]})
    server = start_server()
    tool, _ = call(upstream, server, "fm_shout", '{"text": "stdio"}')
    assert "SHOUTED STDIO" in tool.output


def test_fastmcp_with_a_bearer_token(start_server, upstream, steve_home, http_server):
    url = http_server("bearer", script=FASTMCP)
    write_servers(steve_home, fm={"url": url, "headers": {"Authorization": "Bearer s3cret"}}, refused={"url": url})
    server = start_server()
    assert statuses(server)["refused"]["status"] == "failed"
    tool, _ = call(upstream, server, "fm_shout", '{"text": "bearer"}')
    assert "SHOUTED BEARER" in tool.output


def test_fastmcp_with_oauth(start_server, upstream, steve_home, http_server):
    write_servers(steve_home, fm={"url": http_server("oauth", script=FASTMCP), "auth": "oauth"})
    assert statuses_of(steve_home)["fm"]["status"] == "needs_login"
    assert "fm_shout" in asyncio.run(login(steve_home, "fm", browser))
    server = start_server()
    tool, _ = call(upstream, server, "fm_shout", '{"text": "oauth"}')
    assert "SHOUTED OAUTH" in tool.output


# MCP Apps

APPS = {"command": sys.executable, "args": [str(Path(__file__).with_name("fake_mcp_app.py"))]}


def post(live, path, **body):
    return httpx.post(f"{live.url}/v1/mcp/apps/{path}", json=body, timeout=30)


def test_the_client_says_it_shows_apps(start_server, upstream, steve_home):
    write_servers(steve_home, board=APPS)
    tool, _ = call(upstream, start_server(), "board_supports_apps")
    assert "APPS=True" in tool.output


def test_an_app_tool_says_which_page_it_opens_and_carries_its_result(start_server, upstream, steve_home):
    write_servers(steve_home, board=APPS)
    server = start_server()
    tool, _ = call(upstream, server, "board_show_board", '{"title": "plan"}')
    dumped = tool.model_dump()
    assert dumped["app"] == {"server": "board", "resource_uri": "ui://board/view.html"}
    assert dumped["app_result"]["structuredContent"] == {"title": "PLAN"}
    assert dumped["app_result"]["content"][0]["type"] == "text"


def test_a_tool_without_a_page_has_no_app(start_server, upstream, steve_home):
    write_servers(steve_home, board=APPS)
    tool, _ = call(upstream, start_server(), "board_supports_apps")
    assert "app" not in tool.model_dump()


def test_the_model_is_not_offered_tools_only_for_apps(start_server, upstream, steve_home):
    write_servers(steve_home, board=APPS)
    server = start_server()
    assert "board_refresh_board" not in statuses(server)["board"]["tools"]
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="hello")
    offered = json.dumps(upstream.requests[-1]["body"]["tools"])
    assert "board_show_board" in offered and "board_refresh_board" not in offered


def test_the_page_comes_with_what_it_asks_for(start_server, steve_home):
    write_servers(steve_home, board=APPS)
    page = post(start_server(), "page", server="board", uri="ui://board/view.html").json()
    assert "BOARD_PAGE" in page["html"]
    assert page["csp"]["connectDomains"] == ["https://api.example.test"]
    assert page["prefers_border"] is False


def test_page_requests_are_checked(start_server, steve_home):
    write_servers(steve_home, board=APPS)
    server = start_server()
    assert post(server, "page", server="board", uri="file:///etc/passwd").status_code == 400
    assert post(server, "page", server="absent", uri="ui://x").status_code == 404
    assert post(server, "page", server="board", uri="ui://board/missing").status_code == 502


def test_an_app_can_only_call_tools_marked_for_apps(start_server, steve_home):
    write_servers(steve_home, board=APPS, other=STDIO)
    server = start_server()
    called = post(server, "call", server="board", name="refresh_board", arguments={})
    assert called.status_code == 200 and "REFRESHED" in called.text
    both = post(server, "call", server="board", name="show_board", arguments={"title": "x"})
    assert both.status_code == 200 and both.json()["structuredContent"] == {"title": "X"}
    refused = post(server, "call", server="board", name="model_only", arguments={})
    assert refused.status_code == 403 and "SECRET_RESULT" not in refused.text
    assert post(server, "call", server="board", name="shout", arguments={"text": "x"}).status_code == 403, "another server's tool"
    assert post(server, "call", server="absent", name="x").status_code == 403


def test_an_app_can_read_its_servers_resources(start_server, steve_home):
    write_servers(steve_home, board=APPS)
    read = post(start_server(), "resource", server="board", uri="ui://board/view.html")
    assert read.status_code == 200 and read.json()["contents"][0]["text"].startswith("<!doctype")


def test_an_app_tools_turn_is_saved_and_comes_back(start_server, upstream, steve_home):
    write_servers(steve_home, board=APPS)
    server = start_server(save_conversations=True)
    tool, response = call(upstream, server, "board_show_board", '{"title": "plan"}')

    restarted = start_server(save_conversations=True)
    conversation = response.model_dump()["conversation"]["id"]
    saved = httpx.get(f"{restarted.url}/v1/conversations/{conversation}", timeout=30)
    assert saved.status_code == 200, saved.text
    item = next(item for item in saved.json()["turns"][0]["response"]["output"] if item["type"] == "mcp_call")
    assert item["app"]["resource_uri"] == "ui://board/view.html"
    assert item["app_result"]["structuredContent"] == {"title": "PLAN"}

    deleted = httpx.delete(f"{restarted.url}/v1/conversations/{conversation}", timeout=30)
    assert deleted.status_code == 200, deleted.text
