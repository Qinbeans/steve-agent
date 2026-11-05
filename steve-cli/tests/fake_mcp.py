"""A real MCP server for tests: `stdio`, `http` or `oauth`. The HTTP forms print their port."""

import base64
import hashlib
import json
import socket
import sys
from urllib.parse import urlencode

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

mode = sys.argv[1]
server = FastMCP("fake", stateless_http=True, json_response=True, log_level="ERROR")


@server.tool()
def shout(text: str) -> str:
    """Return the text in capitals."""
    return f"SHOUTED {text.upper()}"


@server.tool()
def explode() -> str:
    """Always fails."""
    raise ValueError("kaboom")


if mode == "stdio":
    server.run("stdio")
    sys.exit()

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
base = f"http://127.0.0.1:{port}"
required = sys.argv[2] if len(sys.argv) > 2 else None
inner = server.streamable_http_app()
codes = {}


def refuse(scope_type):
    return Response(status_code=401, headers={"WWW-Authenticate": scope_type})


class Guard:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = dict(scope["headers"])
        token = headers.get(b"authorization", b"").decode()
        if mode == "oauth" and not token.startswith("Bearer issued-"):
            challenge = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
            return await refuse(challenge)(scope, receive, send)
        if mode == "http" and required and token != f"Bearer {required}":
            return await refuse("Bearer")(scope, receive, send)
        return await inner(scope, receive, send)


async def resource(request):
    return JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})


async def metadata(request):
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
    )


async def register(request):
    body = await request.json()
    return JSONResponse({**body, "client_id": "client-1"}, status_code=201)


async def authorize(request):
    query = request.query_params
    if query.get("deny"):
        return RedirectResponse(f"{query['redirect_uri']}?{urlencode({'error': 'access_denied', 'state': query['state']})}")
    codes["code-1"] = query["code_challenge"]
    return RedirectResponse(f"{query['redirect_uri']}?{urlencode({'code': 'code-1', 'state': query['state']})}")


async def token(request):
    form = await request.form()
    if form["grant_type"] == "authorization_code":
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != codes.get(form["code"]):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse({"access_token": "issued-1", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "refresh-1"})
    if form["grant_type"] == "refresh_token" and form["refresh_token"] == "refresh-1":
        return JSONResponse({"access_token": "issued-2", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "refresh-1"})
    return JSONResponse({"error": "invalid_grant"}, status_code=400)


routes = [
    Route("/.well-known/oauth-protected-resource/mcp", resource),
    Route("/.well-known/oauth-protected-resource", resource),
    Route("/.well-known/oauth-authorization-server", metadata),
    Route("/register", register, methods=["POST"]),
    Route("/authorize", authorize),
    Route("/token", token, methods=["POST"]),
    Mount("/", app=Guard()),
]
app = Starlette(routes=routes, lifespan=lambda _: server.session_manager.run())
print(json.dumps({"port": port}), flush=True)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
