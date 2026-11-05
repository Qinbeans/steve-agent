"""A server built with the fastmcp package: `stdio`, `bearer` or `oauth`. The HTTP forms print their port."""

import json
import socket
import sys

from fastmcp import FastMCP
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from fastmcp.server.auth import StaticTokenVerifier
from mcp.server.auth.settings import ClientRegistrationOptions

mode = sys.argv[1]
auth = None
port = None
if mode != "stdio":
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
if mode == "bearer":
    auth = StaticTokenVerifier(tokens={"s3cret": {"client_id": "tester", "scopes": []}})
if mode == "oauth":
    auth = InMemoryOAuthProvider(
        base_url=f"http://127.0.0.1:{port}",
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )

server = FastMCP("fake", auth=auth)


@server.tool
def shout(text: str) -> str:
    """Return the text in capitals."""
    return f"SHOUTED {text.upper()}"


if mode == "stdio":
    server.run(show_banner=False)
else:
    print(json.dumps({"port": port}), flush=True)
    server.run(transport="http", host="127.0.0.1", port=port, show_banner=False, log_level="ERROR")
