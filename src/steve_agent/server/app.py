import asyncio
import secrets
from collections.abc import AsyncIterator
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
from steve_agent.models import SteveConfig, SteveServerConfig
from steve_agent.server.errors import ApiError, add_error_handlers, error_response
from steve_agent.server.events import EventTranslator
from steve_agent.server.inputs import translate_input
from steve_agent.server.middleware import InstructionsMiddleware, RunContext, TaskNamespaceMiddleware
from steve_agent.server.runner import ChildRun, ChildRuns, Run, RunChannel
from steve_agent.server.schemas import CreateResponseRequest
from steve_agent.server.store import ResponseRecord, ResponseStore, normalize_input_items

MODEL = {"id": "steve-agent", "object": "model", "created": 0, "owned_by": "steve"}


def build_server_agent(config: SteveConfig, home: Path | None = None) -> CompiledStateGraph:
    return build_agent(
        config,
        home=home,
        checkpointer=InMemorySaver(),
        middleware=[InstructionsMiddleware(), TaskNamespaceMiddleware()],
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


def create_app(agent: CompiledStateGraph, config: SteveConfig, *, heartbeat_seconds: float = 15.0) -> FastAPI:
    runs: set[Run] = set()
    store = ResponseStore(agent.checkpointer, config.server.max_conversations)

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

    @app.post("/v1/responses")
    async def create_response(body: CreateResponseRequest, request: Request):
        agent_input = translate_input(body)
        input_items = normalize_input_items(body.input)
        # No await between here and Run(): the thread is pinned before anything else can evict it.
        thread_id, checkpoint_id = store.begin_run(body.previous_response_id)

        def on_open(child: ChildRun) -> None:
            store.track(child.translator.response_id, child.channel, child=True)

        children = ChildRuns(lambda: EventTranslator(model=body.model, store=body.store), on_open)

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
            )
            store.end_run(thread_id, record, *child_records)

        translator = EventTranslator(
            model=body.model,
            instructions=body.instructions,
            metadata=body.metadata,
            previous_response_id=body.previous_response_id,
            store=body.store,
            on_task_call=children.open,
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
        )
        store.track(translator.response_id, run.channel, input_items=input_items)
        runs.add(run)
        run.task.add_done_callback(lambda _: runs.discard(run))
        _cancel_on_disconnect(request, run)

        if body.stream:
            return _sse_response(run.channel, heartbeat_seconds)
        response = (await run.channel.terminal()).response
        if response.status in ("failed", "cancelled"):
            raise ApiError(500, response.error.message, code=response.error.code)
        return _json(response)

    return app
