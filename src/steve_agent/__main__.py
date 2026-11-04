import argparse
import ipaddress
import sys

from langgraph.graph.state import CompiledStateGraph

from steve_agent.agent import build_agent, get_config


def run(agent: CompiledStateGraph):
    print("Welcome to Steve Agent!")
    while message := input("> "):
        if message.lower() == "/quit":
            break
        response = agent.invoke({"messages": message})
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


def serve(host: str | None, port: int | None) -> None:
    # Imported here so that the REPL does not load the web stack.
    import uvicorn

    from steve_agent.server.app import build_server_agent, create_app

    config = get_config()
    host = host or config.server.host
    port = config.server.port if port is None else port
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
    app = create_app(build_server_agent(config), config)
    # The grace period ends streams that would otherwise hold shutdown open; lifespan then cancels their runs.
    uvicorn.run(app, host=host, port=port, workers=1, timeout_graceful_shutdown=5)


def main():
    parser = argparse.ArgumentParser(prog="steve-agent", description="Steve Agent. With no command, starts the REPL.")
    commands = parser.add_subparsers(dest="command")
    serve_parser = commands.add_parser("serve", help="Serve the OpenAI Responses API.")
    serve_parser.add_argument("--host", help="Address to bind. Defaults to server.host from the config.")
    serve_parser.add_argument("--port", type=int, help="Port to bind, 0 for any free one. Defaults to server.port.")
    args = parser.parse_args()

    if args.command == "serve":
        serve(args.host, args.port)
        return
    config = get_config()
    agent = build_agent(config)
    run(agent)


if __name__ == "__main__":
    result = main()
