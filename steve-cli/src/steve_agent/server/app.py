import asyncio
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import Response as HttpResponse
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel
from openai.types.responses import Response
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware

from steve_agent.agent import build_agent
from steve_agent.mcp import McpAppError, McpTools
from steve_agent.models import SteveConfig, SteveServerConfig
from steve_agent.server.conversations import ConversationDb
from steve_agent.server.errors import ApiError, add_error_handlers, error_response
from steve_agent.server.events import EventTranslator
from steve_agent.server.inputs import AgentInput, translate_input
from steve_agent.server.middleware import ContextMeterMiddleware, InstructionsMiddleware, RunContext, TaskNamespaceMiddleware
from steve_agent.server.runner import ChildRun, ChildRuns, Run, RunChannel
from steve_agent.server.schemas import CreateResponseRequest
from steve_agent.server.store import ResponseRecord, ResponseStore, normalize_input_items

MODEL = {"id": "steve-agent", "object": "model", "created": 0, "owned_by": "steve"}


class McpAppResource(BaseModel):
    server: str
    uri: str


class McpAppCall(BaseModel):
    server: str
    name: str
    arguments: dict = {}


def build_server_agent(config: SteveConfig, home: Path | None = None, tools: Sequence = ()) -> CompiledStateGraph:
    return build_agent(
        config,
        home=home,
        tools=tools,
        workspace=Path(config.server.workspace) if config.server.workspace else None,
        checkpointer=InMemorySaver(),
        middleware=[ContextMeterMiddleware(), InstructionsMiddleware(), TaskNamespaceMiddleware()],
        context_schema=RunContext,
        fresh_memory=True,
        conversation_artifacts=True,
    )


async def _host_allowed(scope, receive, send):
    scope["steve.host_allowed"] = True


async def _discard(message):
    pass


class RequestGuard:
    """Host allowlist (DNS rebinding) and API key. Pure ASGI, because BaseHTTPMiddleware hides disconnects."""

    def __init__(self, app, server: SteveServerConfig):
        self.app = app
        # Only used as a predicate, because its own rejection is not our error envelope.
        self._host_check = TrustedHostMiddleware(_host_allowed, allowed_hosts=server.allowed_hosts, www_redirect=False)
        self._authorization = f"Bearer {server.api_key}".encode() if server.api_key else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        await self._host_check(scope, receive, _discard)
        if not scope.get("steve.host_allowed"):
            response = error_response(400, "Invalid Host header.", code="invalid_host")
        elif self._authorized(scope):
            return await self.app(scope, receive, send)
        else:
            response = error_response(401, "Invalid or missing API key.", code="invalid_api_key")
        await response(scope, receive, send)

    def _authorized(self, scope) -> bool:
        path = scope["path"]
        if self._authorization is None or not (path == "/v1" or path.startswith("/v1/")):
            return True
        supplied = Headers(scope=scope).get("authorization", "").encode()
        return secrets.compare_digest(supplied, self._authorization)


def _json(model: BaseModel) -> HttpResponse:
    # warnings=False: events may hold upstream values wider than the SDK's Literals.
    return HttpResponse(model.model_dump_json(warnings=False), media_type="application/json")


async def _sse(channel: RunChannel, heartbeat_seconds: float, starting_after: int = -1) -> AsyncIterator[str]:
    async for event in channel.subscribe(starting_after, idle_timeout=heartbeat_seconds):
        if event is None:
            yield ": ping\n\n"
        else:
            yield f"event: {event.type}\ndata: {event.model_dump_json(warnings=False)}\n\n"


def _sse_response(channel: RunChannel, heartbeat_seconds: float, starting_after: int = -1) -> StreamingResponse:
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        _sse(channel, heartbeat_seconds, starting_after), media_type="text/event-stream", headers=headers
    )


def _cancel_on_disconnect(request: Request, run: Run) -> None:
    # The body is fully read by now, so receive() only returns once the client has gone.
    async def watch():
        while (await request.receive())["type"] != "http.disconnect":
            pass
        run.task.cancel()

    watcher = asyncio.create_task(watch())
    run.task.add_done_callback(lambda _: watcher.cancel())


def create_app(
    agent: CompiledStateGraph,
    config: SteveConfig,
    *,
    heartbeat_seconds: float = 15.0,
    mcp: McpTools | None = None,
    conversations: ConversationDb | None = None,
) -> FastAPI:
    mcp = mcp or McpTools([], [])
    runs: set[Run] = set()
    store = ResponseStore(
        agent.checkpointer,
        config.server.max_conversations,
        config.server.max_conversation_mb,
        db=conversations,
        workspace=config.server.workspace,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        tasks = [run.task for run in runs]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # No docs routes: they would sit outside /v1 and so outside the API key.
    app = FastAPI(title="Steve Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runs = runs
    app.state.store = store
    add_error_handlers(app)
    app.add_middleware(RequestGuard, server=config.server)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/mcp")
    async def mcp_status():
        """How each MCP server's connection went when the server started."""
        return {"servers": mcp.servers}

    async def _app_request(call):
        try:
            return await call
        except McpAppError as error:
            raise ApiError(error.status, str(error), code="mcp_app_error") from error

    @app.post("/v1/mcp/apps/page")
    async def mcp_app_page(body: McpAppResource):
        return await _app_request(mcp.read_ui(body.server, body.uri))

    @app.post("/v1/mcp/apps/resource")
    async def mcp_app_resource(body: McpAppResource):
        return await _app_request(mcp.read_resource(body.server, body.uri))

    @app.post("/v1/mcp/apps/call")
    async def mcp_app_call(body: McpAppCall):
        return await _app_request(mcp.call_app_tool(body.server, body.name, body.arguments))

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [MODEL]}

    @app.get("/v1/models/{model_id}")
    async def model(model_id: str):
        if model_id != MODEL["id"]:
            raise ApiError(404, f"Model '{model_id}' was not found.", param="model", code="model_not_found")
        return MODEL

    def not_found(response_id: str) -> ApiError:
        return ApiError(404, f"Response '{response_id}' was not found.", code="response_not_found")

    @app.get("/v1/responses/{response_id}")
    async def get_response(response_id: str, stream: bool = False, starting_after: int = Query(-1, ge=-1)):
        channel = store.channel(response_id)
        if channel is None:
            raise not_found(response_id)
        # A watcher that hangs up only ends its own subscription; the run belongs to the POST.
        if stream:
            return _sse_response(channel, heartbeat_seconds, starting_after)
        return _json(channel.snapshot())

    @app.delete("/v1/responses/{response_id}")
    async def delete_response(response_id: str):
        if store.channel(response_id) is None:
            raise not_found(response_id)
        if store.is_child(response_id):
            raise ApiError(
                400, "Subagent responses are read-only; delete the parent response.", code="child_response_read_only"
            )
        if store.get(response_id) is None:
            raise ApiError(400, f"Response '{response_id}' is still running.", code="response_in_progress")
        store.delete(response_id)
        await store.collect()
        return {"id": response_id, "object": "response", "deleted": True}

    @app.get("/v1/responses/{response_id}/input_items")
    async def list_input_items(
        response_id: str,
        limit: int = Query(20, ge=1, le=100),
        after: str | None = None,
        order: Literal["asc", "desc"] = "desc",
    ):
        items = store.input_items(response_id)
        if items is None:
            raise not_found(response_id)
        if order == "desc":
            items = items[::-1]
        if after is not None:
            ids = [item["id"] for item in items]
            if after not in ids:
                raise ApiError(400, f"Input item '{after}' was not found.", param="after", code="invalid_cursor")
            items = items[ids.index(after) + 1 :]
        page = items[:limit]
        return {
            "object": "list",
            "data": page,
            "first_id": page[0]["id"] if page else None,
            "last_id": page[-1]["id"] if page else None,
            "has_more": len(items) > limit,
        }

    @app.get("/v1/conversations")
    async def list_conversations():
        """Saved conversations of the current working folder, newest first."""
        return {"object": "list", "data": store.list_conversations()}

    @app.get("/v1/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str):
        conversation = store.conversation(conversation_id)
        if conversation is None:
            raise ApiError(404, f"Conversation '{conversation_id}' was not found.", code="conversation_not_found")
        await store.collect()
        return conversation

    @app.delete("/v1/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str):
        if not store.delete_conversation(conversation_id):
            raise ApiError(404, f"Conversation '{conversation_id}' was not found.", code="conversation_not_found")
        await store.collect()
        return {"id": conversation_id, "object": "conversation", "deleted": True}

    @app.post("/v1/responses")
    async def create_response(body: CreateResponseRequest, request: Request):
        if body.mode == "manual" and not body.stream:
            raise ApiError(400, "Manual mode asks questions during the run, so it needs stream: true.", param="mode", code="mode_needs_stream")
        agent_input = translate_input(body, max_asset_bytes=config.server.max_asset_mb * 1024 * 1024)
        run = start_run(body, agent_input, normalize_input_items(body.input), request)
        if body.stream:
            return _sse_response(run.channel, heartbeat_seconds)
        return await final_response(run)

    @app.post("/v1/responses/{response_id}/questions/{question_id}")
    async def answer_question(response_id: str, question_id: str, body: dict):
        """The person's answer to an approval or an elicitation that a run is waiting on."""
        run = next((run for run in runs if run.response_id == response_id), None)
        if run is None or not run.interaction.answer(question_id, body):
            raise ApiError(404, f"Question '{question_id}' is not open on this response.", code="question_not_found")
        return {"id": question_id, "answered": True}

    @app.post("/v1/responses/{response_id}/compact")
    async def compact_response(response_id: str, request: Request):
        """Compact a conversation. The result is a new response to continue from."""
        if store.channel(response_id) is None:
            raise not_found(response_id)
        body = CreateResponseRequest(input="compact", previous_response_id=response_id)
        return await final_response(start_run(body, AgentInput([], None), [], request, compact=True))

    async def final_response(run: Run) -> HttpResponse:
        response = (await run.channel.terminal()).response
        if response.status in ("failed", "cancelled"):
            raise ApiError(500, response.error.message, code=response.error.code)
        return _json(response)

    def start_run(
        body: CreateResponseRequest,
        agent_input: AgentInput,
        input_items: list[dict],
        request: Request,
        compact: bool = False,
    ) -> Run:
        # No await between here and Run(): the thread is pinned before anything else can evict it.
        thread_id, checkpoint_id = store.begin_run(body.previous_response_id)

        def on_open(child: ChildRun) -> None:
            store.track(child.translator.response_id, child.channel, child=True)

        children = ChildRuns(lambda: EventTranslator(model=body.model, store=body.store, tool_apps=mcp.apps, conversation_id=thread_id), on_open)

        def on_end(response: Response, head: str | None, ended: list[ChildRun]) -> None:
            store.untrack([response.id, *(child.translator.response_id for child in ended)])
            # A cancelled run is not stored; its dirty checkpoint is never referenced.
            if not body.store or response.status == "cancelled":
                store.end_run(thread_id)
                return
            child_records = [
                ResponseRecord(
                    thread_id=thread_id,
                    checkpoint_id=None,
                    response=child.channel.snapshot(),
                    input_items=[],
                    parent_id=None,
                    channel=child.channel,
                    child_of=response.id,
                )
                for child in ended
            ]
            record = ResponseRecord(
                thread_id=thread_id,
                checkpoint_id=head,
                response=response,
                input_items=input_items,
                parent_id=body.previous_response_id,
                channel=run.channel,
                children=[child.response.id for child in child_records],
                compaction=compact,
            )
            store.end_run(thread_id, record, *child_records)

        translator = EventTranslator(
            model=body.model,
            instructions=body.instructions,
            metadata=body.metadata,
            previous_response_id=body.previous_response_id,
            store=body.store,
            on_task_call=children.open,
            tool_apps=mcp.apps,
            conversation_id=thread_id,
        )
        run = Run(
            agent,
            translator,
            agent_input.messages,
            agent_input.instructions,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            on_end=on_end,
            after_end=store.collect,
            children=children,
            retries=config.server.response_retries,
            files=agent_input.files,
            compact=compact,
            context_limit=config.model.max_input_tokens,
            mode=body.mode,
            streaming=body.stream,
        )
        store.track(translator.response_id, run.channel, input_items=input_items)
        runs.add(run)
        run.task.add_done_callback(lambda _: runs.discard(run))
        _cancel_on_disconnect(request, run)
        return run

    return app
