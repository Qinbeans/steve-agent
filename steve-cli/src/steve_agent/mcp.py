"""MCP servers: what mcp.json lists, connecting to them, and signing in to the ones that use OAuth.

Servers live in ~/.config/steve/mcp.json, in the usual {"mcpServers": {name: ...}} shape:
{"command": ..., "args": [...], "env": {...}} runs a local server, and {"url": ..., "headers": {...}}
reaches a remote one. A bearer token is an Authorization header, and "auth": "oauth" signs in with
`steve-agent mcp login NAME`, which keeps its tokens in ~/.config/steve/mcp-auth/.
"""

import asyncio
import base64
import functools
import json
import os
import re
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from langchain_core.tools import BaseTool, ToolException
from langchain_mcp_adapters import sessions as adapter_sessions
from langchain_mcp_adapters.callbacks import Callbacks
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp import ClientSession, types
from mcp.client.auth import OAuthClientProvider, TokenStorage
from steve_agent.interaction import current_interaction
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

CONNECT_SECONDS = 20
LOGIN_SECONDS = 300
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SIGNED_IN = b"<!doctype html><title>Steve</title><p>You are signed in. You can close this window.</p>"
_FAILED = b"<!doctype html><title>Steve</title><p>Signing in failed. You can close this window.</p>"


class NeedsLogin(Exception):
    """An OAuth server has no usable tokens, and only `steve-agent mcp login` can get them."""


async def _on_elicitation(_session, params, context) -> types.ElicitResult:
    """Puts a server's question to the person. With nobody to ask, as in the terminal chat, it is declined."""
    interaction = current_interaction.get()
    if interaction is None:
        return types.ElicitResult(action="decline")
    details = {"server": context.server_name, "mode": getattr(params, "mode", "form"), "message": params.message}
    if details["mode"] == "url":
        details["url"] = params.url
    else:
        details["schema"] = params.requestedSchema
    answer = await interaction.ask("elicitation", details)
    if answer is None:
        return types.ElicitResult(action="cancel")
    action = answer.get("action")
    if action not in ("accept", "decline", "cancel"):
        return types.ElicitResult(action="cancel")
    return types.ElicitResult(action=action, content=answer.get("content") if action == "accept" else None)


_CALLBACKS = Callbacks(on_elicitation=_on_elicitation)
UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME_TYPE = "text/html;profile=mcp-app"


class _UiClientSession(ClientSession):
    """Tells servers this client shows MCP Apps. The SDK has no option for declaring an extension."""

    async def send_request(self, request, result_type, *args, **kwargs):
        if isinstance(request.root, types.InitializeRequest):
            request.root.params.capabilities.extensions = {UI_EXTENSION: {"mimeTypes": [UI_MIME_TYPE]}}
        return await super().send_request(request, result_type, *args, **kwargs)


adapter_sessions.ClientSession = _UiClientSession


class McpAppError(Exception):
    """A request from an app that cannot be met. `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class McpTools:
    tools: list[BaseTool]  # what the model can call
    servers: list[dict]  # what /v1/mcp reports; holds no secrets
    apps: dict[str, dict] = field(default_factory=dict)  # tool name -> which UI it opens
    connections: dict[str, dict] = field(default_factory=dict)
    app_callable: dict[str, set[str]] = field(default_factory=dict)  # server -> tools its apps may call

    def _session(self, server: str):
        if server not in self.connections:
            raise McpAppError(404, f"There is no connected MCP server called {server}.")
        return MultiServerMCPClient({server: self.connections[server]}, callbacks=_CALLBACKS).session(server)

    async def read_resource(self, server: str, uri: str) -> dict:
        """One resource from a server, for its app."""
        try:
            async with self._session(server) as session:
                result = await session.read_resource(uri)
        except McpAppError:
            raise
        except Exception as error:
            raise McpAppError(502, f"Could not read {uri}: {_reason(error)}") from error
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def read_ui(self, server: str, uri: str) -> dict:
        """An app's page, with what its _meta.ui asks of the host."""
        if not uri.startswith("ui://"):
            raise McpAppError(400, "An app's page has to be a ui:// resource.")
        contents = (await self.read_resource(server, uri)).get("contents") or []
        page = next((item for item in contents if str(item.get("mimeType", "")).startswith(UI_MIME_TYPE)), None)
        if page is None:
            raise McpAppError(502, f"{uri} is not an MCP App page.")
        html = page.get("text")
        if html is None:
            html = base64.b64decode(page.get("blob", "")).decode(errors="replace")
        ui = (page.get("_meta") or {}).get("ui") or {}
        return {"html": html, "csp": ui.get("csp"), "prefers_border": ui.get("prefersBorder")}

    async def call_app_tool(self, server: str, name: str, arguments: dict) -> dict:
        """A call from an app. It can only reach its own server's tools marked for apps."""
        if name not in self.app_callable.get(server, ()):
            raise McpAppError(403, f"Apps cannot call {name} on {server}.")
        try:
            async with self._session(server) as session:
                result = await session.call_tool(name, arguments)
        except McpAppError:
            raise
        except Exception as error:
            raise McpAppError(502, f"The MCP server failed: {_reason(error)}") from error
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)


class FileTokenStorage(TokenStorage):
    """Tokens and the registered client in one file only this user can read."""

    def __init__(self, path: Path):
        self._path = path

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, **changes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self._path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as file:
            json.dump({**self._read(), **changes}, file)
        temporary.replace(self._path)

    async def get_tokens(self) -> OAuthToken | None:
        tokens = self._read().get("tokens")
        return OAuthToken.model_validate(tokens) if tokens else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._write(tokens=tokens.model_dump(mode="json"))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        info = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._write(client_info=client_info.model_dump(mode="json"))


def token_file(home: Path, name: str) -> Path:
    return home / "mcp-auth" / f"{name}.json"


async def _no_browser(url: str) -> None:
    raise NeedsLogin


async def _no_callback() -> tuple[str, str | None]:
    raise NeedsLogin


def _oauth(home: Path, name: str, url: str, redirect_uri: str, redirect_handler, callback_handler) -> OAuthClientProvider:
    return OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="Steve Agent",
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=FileTokenStorage(token_file(home, name)),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=LOGIN_SECONDS,
    )


def _connection(home: Path, name: str, spec: dict) -> dict:
    if not _NAME.match(name):
        raise ValueError("The name can only use letters, numbers, _ and -.")
    if not isinstance(spec, dict):
        raise ValueError("A server must be an object.")
    if spec.get("command"):
        connection = {"transport": "stdio", "command": spec["command"], "args": spec.get("args") or []}
        if spec.get("env"):
            connection["env"] = {**os.environ, **spec["env"]}
        if spec.get("cwd"):
            connection["cwd"] = spec["cwd"]
        return connection
    if spec.get("url"):
        transport = "sse" if spec.get("type") == "sse" else "streamable_http"
        connection = {"transport": transport, "url": spec["url"]}
        if spec.get("headers"):
            connection["headers"] = spec["headers"]
        if spec.get("auth") == "oauth":
            connection["auth"] = _oauth(home, name, spec["url"], "http://127.0.0.1/callback", _no_browser, _no_callback)
        return connection
    raise ValueError("A server needs a command or a url.")


def _read_servers(home: Path) -> dict:
    """Raises ValueError with something to show if the file cannot be used."""
    try:
        data = json.loads((home / "mcp.json").read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError(f"Could not read mcp.json: {error}") from error
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("mcp.json needs an mcpServers object.")
    return servers


def _leaf(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return error


def _reason(error: BaseException) -> str:
    error = _leaf(error)
    if isinstance(error, TimeoutError):
        return f"No answer after {CONNECT_SECONDS} seconds."
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 401:
        return "The server refused access (401). It needs a bearer token or OAuth."
    return str(error)[:500] or type(error).__name__


def _resilient(tool: BaseTool) -> BaseTool:
    """A server that fails during a call gives the agent an error to read, instead of ending the run."""
    call = tool.coroutine

    @functools.wraps(call)
    async def guarded(*args, **kwargs):
        try:
            return await call(*args, **kwargs)
        except ToolException:
            raise
        except Exception as error:
            if isinstance(_leaf(error), NeedsLogin):
                raise ToolException("This server needs signing in again. Tell the user.") from error
            raise ToolException(f"The MCP server failed: {_reason(error)}") from error

    tool.coroutine = guarded
    tool.handle_tool_error = True
    return tool


def _ui_of(tool: BaseTool) -> tuple[str | None, list[str]]:
    """The tool's UI page if it has one, and who can use the tool: the model, apps or both."""
    meta = (tool.metadata or {}).get("_meta") or {}
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    uri = ui.get("resourceUri") or meta.get("ui/resourceUri")
    return uri if isinstance(uri, str) and uri.startswith("ui://") else None, ui.get("visibility") or ["model", "app"]


async def _connect_one(home: Path, name: str, spec: dict, found: McpTools) -> tuple[list[BaseTool], dict]:
    status = {"name": name, "status": "connected", "tools": [], "error": None}
    try:
        connection = _connection(home, name, spec)
        client = MultiServerMCPClient({name: connection}, tool_name_prefix=True, callbacks=_CALLBACKS)
        tools = await asyncio.wait_for(client.get_tools(server_name=name), CONNECT_SECONDS)
    except Exception as error:
        if isinstance(_leaf(error), NeedsLogin):
            return [], {**status, "status": "needs_login", "error": "Sign in to use this server."}
        return [], {**status, "status": "failed", "error": _reason(error)}
    found.connections[name] = connection
    found.app_callable[name] = set()
    model_tools = []
    for tool in tools:
        uri, visibility = _ui_of(tool)
        if "app" in visibility:
            found.app_callable[name].add(tool.name.removeprefix(f"{name}_"))
        if "model" not in visibility:
            continue
        if uri:
            found.apps[tool.name] = {"server": name, "resource_uri": uri}
        model_tools.append(_resilient(tool))
    return model_tools, {**status, "tools": [tool.name for tool in model_tools]}


async def _connect_all(home: Path) -> McpTools:
    try:
        servers = _read_servers(home)
    except ValueError as error:
        return McpTools([], [{"name": "mcp.json", "status": "failed", "tools": [], "error": str(error)}])
    found = McpTools([], [])
    results = await asyncio.gather(*(_connect_one(home, name, spec, found) for name, spec in servers.items()))
    found.tools = [tool for tools, _ in results for tool in tools]
    found.servers = [status for _, status in results]
    return found


def load_mcp(home: Path) -> McpTools:
    """Connects to every server. One that fails is reported, and the agent starts without it."""
    return asyncio.run(_connect_all(home))


async def login(home: Path, name: str, open_browser: Callable[[str], object] = webbrowser.open) -> list[str]:
    """Runs the browser sign-in for a server, and returns its tools. Raises ValueError with what went wrong."""
    spec = _read_servers(home).get(name)
    if not isinstance(spec, dict) or spec.get("auth") != "oauth" or not spec.get("url"):
        raise ValueError(f"{name} is not a server in mcp.json that uses OAuth.")

    loop = asyncio.get_running_loop()
    answer: asyncio.Future[tuple[str, str | None]] = loop.create_future()

    async def on_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            target = (await reader.readline()).decode(errors="replace").split(" ")[1]
            while (await reader.readline()).strip():
                pass
            parts = urlsplit(target)
            query = parse_qs(parts.query)
            if parts.path != "/callback":
                writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                return
            code = query.get("code", [None])[0]
            body = _SIGNED_IN if code else _FAILED
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            if not answer.done():
                if code:
                    answer.set_result((code, query.get("state", [None])[0]))
                else:
                    answer.set_exception(ValueError(f"The server refused: {query.get('error', ['unknown'])[0]}."))
        except IndexError:
            pass
        finally:
            await writer.drain()
            writer.close()

    callback_server = await asyncio.start_server(on_request, "127.0.0.1", 0)
    port = callback_server.sockets[0].getsockname()[1]

    async def redirect(url: str) -> None:
        print(f"Opening your browser to sign in to {name}. If it does not open, visit:\n{url}", flush=True)
        await asyncio.to_thread(open_browser, url)

    # A new redirect address each time, so an earlier registration cannot be reused.
    token_file(home, name).unlink(missing_ok=True)
    provider = _oauth(home, name, spec["url"], f"http://127.0.0.1:{port}/callback", redirect, lambda: answer)
    connection = {"transport": "sse" if spec.get("type") == "sse" else "streamable_http", "url": spec["url"], "auth": provider}
    try:
        async with callback_server:
            client = MultiServerMCPClient({name: connection}, tool_name_prefix=True)
            tools = await asyncio.wait_for(client.get_tools(server_name=name), LOGIN_SECONDS)
    except TimeoutError:
        raise ValueError("Signing in timed out.") from None
    except Exception as error:
        raise ValueError(f"Signing in failed: {_reason(error)}") from error
    return [tool.name for tool in tools]
