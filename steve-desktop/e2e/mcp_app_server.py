"""A real FastMCP server with an MCP App for the end-to-end test. Its page tries to leave its sandbox and reports what happened."""

from dataclasses import dataclass

from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig

server = FastMCP("board")
PAGE = "ui://board/view.html"

HTML = """<!doctype html>
<html><body style="font-family: sans-serif; color: gray"><p id="note">Board app</p><script>
let next = 1, input = null, protocol = null, theme = null;
const pending = new Map();
const send = (message) => parent.postMessage({ jsonrpc: "2.0", ...message }, "*");
const rpc = (method, params = {}) => new Promise((resolve, reject) => {
  const id = next++;
  pending.set(id, { resolve, reject });
  send({ id, method, params });
});
async function probe(attempt) {
  try { await attempt(); return "ALLOWED"; } catch { return "blocked"; }
}
async function report(result) {
  const out = {
    ipc: await probe(() => window.__TAURI_INTERNALS__.invoke("get_workspace")),
    ipcFetch: await probe(() => fetch("http://ipc.localhost/get_workspace", { method: "POST", body: "{}" })),
    parent: await probe(async () => parent.document.title),
    storage: await probe(async () => localStorage.getItem("x")),
    outside: await probe(() => fetch("https://example.com/")),
    modelOnly: await probe(() => rpc("tools/call", { name: "model_only", arguments: {} })),
  };
  const refreshed = await rpc("tools/call", { name: "refresh_board", arguments: {} });
  send({ method: "ui/notifications/size-changed", params: { width: 400, height: 120 } });
  await rpc("ui/message", { role: "user", content: { type: "text", text: JSON.stringify({
    ...out, protocol, theme, title: result.structuredContent.title, input: input.title,
    refreshed: refreshed.content[0].text,
  }) } });
}
addEventListener("message", (event) => {
  const message = event.data;
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    message.error ? reject(new Error(message.error.message)) : resolve(message.result);
  }
  if (message.method === "ui/notifications/tool-input") input = message.params.arguments;
  if (message.method === "ui/notifications/tool-result") report(message.params);
});
rpc("ui/initialize", { protocolVersion: "2026-01-26", appCapabilities: {}, appInfo: { name: "board", version: "1" } }).then((result) => {
  protocol = result.protocolVersion;
  theme = ["dark", "light"].includes(result.hostContext.theme) ? "dark-or-light" : result.hostContext.theme;
  send({ method: "ui/notifications/initialized", params: {} });
});
</script></body></html>"""


@server.tool(app=AppConfig(resource_uri=PAGE))
def show_board(title: str) -> dict:
    """Show a board."""
    return {"title": title.upper()}


@server.tool(app=AppConfig(resource_uri=PAGE, visibility=["app"]))
def refresh_board() -> str:
    """Only the board's page calls this."""
    return "REFRESHED"


@server.tool(app=AppConfig(resource_uri=PAGE, visibility=["model"]))
def model_only() -> str:
    """Apps cannot call this."""
    return "SECRET_RESULT"


@server.resource(PAGE)
def board_page() -> str:
    return HTML


@dataclass
class Ticket:
    title: str
    urgent: bool = False
    count: int = 1


@server.tool
async def pick_colour(ctx: Context) -> str:
    """Asks the person to pick a colour."""
    result = await ctx.elicit("Which **colour** do you want?", response_type=["red", "green"])
    return f"{result.action}:{getattr(result, 'data', None)}"


@server.tool
async def file_ticket(ctx: Context) -> str:
    """Asks the person for a ticket's details."""
    result = await ctx.elicit("Describe the ticket.", response_type=Ticket)
    return f"{result.action}:{getattr(result, 'data', None)}"


@server.tool
async def pick_name(ctx: Context) -> str:
    """Offers names, and lets the person type their own."""
    schema = {
        "type": "object",
        "properties": {"name": {"title": "Name", "anyOf": [{"const": "Ada", "title": "Ada"}, {"const": "Grace", "title": "Grace"}, {"type": "string"}]}},
        "required": ["name"],
    }
    result = await ctx.session.elicit(message="Who should it be?", requestedSchema=schema)
    return f"{result.action}:{result.content}"


server.run(show_banner=False)
