"""A fastmcp server with an MCP App, like a real one. `stdio` only."""

from dataclasses import dataclass

from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig, ResourceCSP, UI_EXTENSION_ID

server = FastMCP("apps")
PAGE = "ui://board/view.html"
HTML = "<!doctype html><title>Board</title><p id=mark>BOARD_PAGE</p>"


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


@server.tool
def supports_apps(ctx: Context) -> str:
    """Does the client say it shows apps?"""
    return f"APPS={ctx.client_supports_extension(UI_EXTENSION_ID)}"


@server.resource(PAGE, app=AppConfig(csp=ResourceCSP(connect_domains=["https://api.example.test"]), prefers_border=False))
def board_page() -> str:
    return HTML


@server.tool
async def pick_colour(ctx: Context) -> str:
    """Asks the person to pick a colour."""
    result = await ctx.elicit("Which colour?", response_type=["red", "green"])
    return f"{result.action}:{getattr(getattr(result, 'data', None), 'value', getattr(result, 'data', None))}"


@dataclass
class Ticket:
    title: str
    urgent: bool = False
    count: int = 1


@server.tool
async def file_ticket(ctx: Context) -> str:
    """Asks the person for a ticket's details."""
    result = await ctx.elicit("Describe the ticket.", response_type=Ticket)
    return f"{result.action}:{getattr(result, 'data', None)}"


@server.tool(annotations={"readOnlyHint": True})
def look_only() -> str:
    """Changes nothing."""
    return "LOOKED"


@server.tool
def change_something() -> str:
    """Changes something."""
    return "CHANGED"


server.run(show_banner=False)
