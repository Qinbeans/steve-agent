import argparse
import asyncio
import ipaddress
import subprocess
import sys
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from steve_agent.agent import build_agent, default_home, get_config
from steve_agent.mcp import load_mcp, login
from steve_agent.web import browser_installed


def run(agent: CompiledStateGraph):
    print("Welcome to Steve Agent!")
    while message := input("> "):
        if message.lower() == "/quit":
            break
        # Async, because MCP tools have no synchronous form.
        response = asyncio.run(agent.ainvoke({"messages": message}))
        print(response["messages"][-1].content[-1]["text"])


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_header_allowed(host: str, allowed_hosts: list[str]) -> bool:
    if host in ("0.0.0.0", "::") or "*" in allowed_hosts:
        return True
    return (f"[{host}]" if ":" in host else host) in allowed_hosts


def _resolve_workspace(workspace: str) -> Path:
    path = Path(workspace).expanduser().resolve()
    if not path.is_dir():
        sys.exit(f"Workspace {path} is not a directory.")
    return path


def serve(host: str | None, port: int | None, workspace: str | None) -> None:
    # Imported here so that the REPL does not load the web stack.
    import uvicorn

    from steve_agent.server.app import build_server_agent, create_app
    from steve_agent.server.conversations import open_conversations

    config = get_config()
    host = host or config.server.host
    port = config.server.port if port is None else port
    workspace = workspace or config.server.workspace
    # Only the terminal chat works where it was started: a server's directory is an explicit choice.
    config.server.workspace = str(_resolve_workspace(workspace)) if workspace else None
    if not _is_loopback(host) and not config.server.api_key:
        sys.exit(
            f"Refusing to serve on non-loopback host {host!r} without server.api_key: "
            "the agent can write files. Set server.api_key in the config or use a loopback host."
        )
    if not _host_header_allowed(host, config.server.allowed_hosts):
        print(
            f"Warning: {host!r} is not in server.allowed_hosts, so requests sent to that address get 400 "
            "'Invalid Host header.'. Add it, unless clients reach the server under an allowed name.",
            file=sys.stderr,
        )
    mcp = load_mcp(default_home())
    for server in mcp.servers:
        if server["error"]:
            print(f"MCP server {server['name']}: {server['error']}", file=sys.stderr)
    conversations = open_conversations(default_home()) if config.server.save_conversations else None
    app = create_app(build_server_agent(config, tools=mcp.tools), config, mcp=mcp, conversations=conversations)
    # The grace period ends streams that would otherwise hold shutdown open; lifespan then cancels their runs.
    uvicorn.run(app, host=host, port=port, workers=1, timeout_graceful_shutdown=5)


def web_command(command: str) -> None:
    config = get_config()
    if command == "setup":
        if config.web.browser:
            print(f"Using {config.web.browser}, so there is nothing to download.")
        else:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--only-shell"], check=True)
    if not browser_installed(config.web):
        print("The browser could not start.", file=sys.stderr)
        sys.exit(1)
    print("The browser works.")


def main():
    parser = argparse.ArgumentParser(prog="steve-agent", description="Steve Agent. With no command, starts the REPL.")
    commands = parser.add_subparsers(dest="command")
    serve_parser = commands.add_parser("serve", help="Serve the OpenAI Responses API.")
    serve_parser.add_argument("--host", help="Address to bind. Defaults to server.host from the config.")
    serve_parser.add_argument("--port", type=int, help="Port to bind, 0 for any free one. Defaults to server.port.")
    serve_parser.add_argument("--workspace", help="Directory the agent works in. Defaults to server.workspace, or none.")
    mcp_parser = commands.add_parser("mcp", help="Manage MCP servers.")
    mcp_commands = mcp_parser.add_subparsers(dest="mcp_command", required=True)
    login_parser = mcp_commands.add_parser("login", help="Sign in to a server in mcp.json that uses OAuth.")
    login_parser.add_argument("name", help="The server's name in mcp.json.")
    web_parser = commands.add_parser("web", help="Set up the browser the agent reads the web with.")
    web_commands = web_parser.add_subparsers(dest="web_command", required=True)
    web_commands.add_parser("setup", help="Download the browser, unless Settings names one of your own.")
    web_commands.add_parser("status", help="Check that the browser starts.")
    args = parser.parse_args()

    if args.command == "serve":
        serve(args.host, args.port, args.workspace)
        return
    if args.command == "web":
        web_command(args.web_command)
        return
    if args.command == "mcp":
        try:
            tools = asyncio.run(login(default_home(), args.name))
        except ValueError as error:
            sys.exit(str(error))
        print(f"Signed in to {args.name}, which has {len(tools)} tools.")
        return
    config = get_config()
    agent = build_agent(config, workspace=Path.cwd(), tools=load_mcp(default_home()).tools)
    run(agent)


if __name__ == "__main__":
    result = main()
