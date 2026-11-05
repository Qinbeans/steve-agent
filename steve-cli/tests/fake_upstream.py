"""Loopback fake of OpenAI /v1/responses and /v1/chat/completions, with real SSE wire shapes.

Each model call consumes one Step from `steps`; with none queued it echoes the last user text.
`routes` overrides that for calls whose last user text contains a key, so a parallel subagent gets its own script.
"""

import json
import select
import socket
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Step:
    text: str | None = None
    reasoning: bool = False
    tools: Sequence[tuple[str, str]] = ()
    end: str = "completed"  # completed, failed, incomplete or cut; on chat: length, content_filter or cut
    refusal: str | None = None  # Responses only: the model refuses instead of answering
    http_error: tuple[int, str] | None = None
    input_tokens: int | None = None  # report this usage instead of the small default
    no_sdk_retry: bool = False  # tell the inner ChatOpenAI not to retry this error itself
    hold: bool = False  # after the first text delta, wait for release or for the client to hang up
    barrier: threading.Barrier | None = None  # wait for other calls, to force overlapping runs


def _usage(n):
    return {
        "input_tokens": 100 * n,
        "input_tokens_details": {"cached_tokens": 3},
        "output_tokens": 20,
        "output_tokens_details": {"reasoning_tokens": 2},
        "total_tokens": 100 * n + 20,
    }


def _step_usage(step, n):
    usage = _usage(n)
    if step.input_tokens:
        usage["input_tokens"] = step.input_tokens
        usage["total_tokens"] = step.input_tokens + usage["output_tokens"]
    return usage


def last_user_text(body):
    for item in reversed(body.get("input") or body.get("messages") or []):
        if item.get("role") == "user":
            content = item["content"]
            return content if isinstance(content, str) else "".join(part.get("text", "") for part in content)
    return ""


def _llama_rejects(body):
    """What the real llama.cpp upstream answers to replayed items it cannot read."""
    items = body.get("input")
    for item in items if isinstance(items, list) else []:
        if item.get("type") == "reasoning" and not item.get("content"):
            return (400, "item['content'] is not an array")
        if item.get("type") == "function_call" and not item.get("arguments"):
            return (500, "Failed to parse tool call arguments as JSON")
    return None


class FakeUpstream:
    def __init__(self):
        self.requests = []
        self.steps = deque()
        self.routes = {}
        self.omit_stream_indexes = False  # like llama.cpp behind a gateway
        self.llama_like = False  # no indexes, raw reasoning text, and strict about what is replayed
        self.holding = threading.Event()
        self.release = threading.Event()
        self.hung_up = threading.Event()
        self._lock = threading.Lock()
        self._n = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                with fake._lock:
                    fake.requests.append({"path": self.path, "body": body})
                    fake._n += 1
                    n = fake._n
                    queue = next((q for key, q in fake.routes.items() if key in last_user_text(body)), fake.steps)
                    step = queue.popleft() if queue else Step()
                try:
                    if step.barrier:
                        step.barrier.wait(timeout=10)
                    if fake.llama_like and (error := _llama_rejects(body)):
                        fake._http_error(self, Step(http_error=error))
                    elif step.http_error:
                        fake._http_error(self, step)
                    elif self.path.endswith("/chat/completions"):
                        fake._chat(self, body, step, n)
                    elif body.get("stream"):
                        fake._stream(self, body, step, n)
                    else:
                        fake._plain(self, body, step, n)
                except OSError:
                    self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        threading.Thread(target=self._server.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.release.set()
        self._server.shutdown()
        self._server.server_close()

    def body_text(self, index=-1):
        return json.dumps(self.requests[index]["body"])

    def _wait_for_release(self, handler):
        self.holding.set()
        while not self.release.wait(0.02):
            readable, _, _ = select.select([handler.connection], [], [], 0)
            if readable and not handler.connection.recv(1, socket.MSG_PEEK):
                self.hung_up.set()
                raise ConnectionAbortedError

    def _items(self, step, n, body):
        items = []
        if step.reasoning:
            items.append(
                {
                    "id": f"rs_{n}",
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "Thinking part A."},
                        {"type": "summary_text", "text": "Thinking part B."},
                    ],
                }
            )
        if step.refusal:
            items.append(
                {
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "refusal", "refusal": step.refusal}],
                }
            )
            return items
        text = step.text if step.text is not None else f"echo: {last_user_text(body)}"
        if text:
            items.append(
                {
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
        for j, (name, arguments) in enumerate(step.tools):
            items.append(
                {
                    "id": f"fc_{n}_{j}",
                    "type": "function_call",
                    "call_id": f"call_{n}_{j}",
                    "name": name,
                    "arguments": arguments,
                    "status": "completed",
                }
            )
        return items

    def _response(self, n, body, status, output, usage):
        return {
            "id": f"resp_{n}",
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": body["model"],
            "output": output,
            "usage": usage,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "metadata": {},
            "parallel_tool_calls": True,
            "temperature": 1.0,
            "top_p": 1.0,
            "tool_choice": "auto",
            "tools": [],
            "service_tier": "default",
            "store": body.get("store", True),
        }

    def _http_error(self, handler, step):
        status, message = step.http_error
        error = {"message": message, "type": "invalid_request_error", "param": None, "code": None}
        self._send_json(handler, status, {"error": error}, no_retry=step.no_sdk_retry)

    def _send_json(self, handler, status, payload, no_retry=False):
        data = json.dumps(payload).encode()
        handler.send_response(status)
        if no_retry:
            handler.send_header("x-should-retry", "false")
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _plain(self, handler, body, step, n):
        self._send_json(handler, 200, self._response(n, body, "completed", self._items(step, n, body), _step_usage(step, n)))

    def _start_sse(self, handler):
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("connection", "close")
        handler.end_headers()
        handler.close_connection = True

    def _stream(self, handler, body, step, n):
        self._start_sse(handler)
        sequence = [0]

        def emit(event_type, **fields):
            fields = {"type": event_type, "sequence_number": sequence[0], **fields}
            sequence[0] += 1
            if self.omit_stream_indexes or self.llama_like:
                fields = {k: v for k, v in fields.items() if k not in ("output_index", "content_index")}
            handler.wfile.write(f"event: {event_type}\ndata: {json.dumps(fields)}\n\n".encode())
            handler.wfile.flush()

        items = self._items(step, n, body)
        emit("response.created", response=self._response(n, body, "in_progress", [], None))
        emit("response.in_progress", response=self._response(n, body, "in_progress", [], None))
        for index, item in enumerate(items):
            where = {"item_id": item["id"], "output_index": index}
            if item["type"] == "reasoning" and self.llama_like:
                text = " ".join(part["text"] for part in item["summary"])
                bare = {k: v for k, v in item.items() if k != "summary"}
                emit(
                    "response.output_item.added",
                    output_index=index,
                    item={**bare, "summary": [], "content": [], "status": "in_progress"},
                )
                for word in text.split(" "):
                    emit("response.reasoning_text.delta", delta=word + " ", **where)
                done = {"type": "reasoning_text", "text": text}
                emit(
                    "response.output_item.done",
                    output_index=index,
                    item={**bare, "summary": [], "content": [done], "encrypted_content": "", "status": "completed"},
                )
                continue
            if item["type"] == "reasoning":
                emit("response.output_item.added", output_index=index, item={**item, "summary": []})
                for part_index, part in enumerate(item["summary"]):
                    part_where = {**where, "summary_index": part_index}
                    emit(
                        "response.reasoning_summary_part.added",
                        part={"type": "summary_text", "text": ""},
                        **part_where,
                    )
                    emit("response.reasoning_summary_text.delta", delta=part["text"], **part_where)
                    emit("response.reasoning_summary_text.done", text=part["text"], **part_where)
                    emit("response.reasoning_summary_part.done", part=part, **part_where)
            elif item["type"] == "message":
                emit(
                    "response.output_item.added",
                    output_index=index,
                    item={**item, "content": [], "status": "in_progress"},
                )
                part = item["content"][0]
                if part["type"] == "refusal":
                    content_where = {**where, "content_index": 0}
                    emit("response.content_part.added", part={"type": "refusal", "refusal": ""}, **content_where)
                    emit("response.refusal.delta", delta=part["refusal"], **content_where)
                    emit("response.refusal.done", refusal=part["refusal"], **content_where)
                    emit("response.content_part.done", part=part, **content_where)
                    emit("response.output_item.done", output_index=index, item=item)
                    continue
                text = part["text"]
                content_where = {**where, "content_index": 0}
                emit(
                    "response.content_part.added",
                    part={"type": "output_text", "text": "", "annotations": []},
                    **content_where,
                )
                emit("response.output_text.delta", delta=text[:5], logprobs=[], **content_where)
                if step.hold:
                    self._wait_for_release(handler)
                emit("response.output_text.delta", delta=text[5:], logprobs=[], **content_where)
                emit("response.output_text.done", text=text, logprobs=[], **content_where)
                emit("response.content_part.done", part=part, **content_where)
            else:
                emit(
                    "response.output_item.added",
                    output_index=index,
                    item={**item, "arguments": "", "status": "in_progress"},
                )
                emit("response.function_call_arguments.delta", delta=item["arguments"], **where)
                emit(
                    "response.function_call_arguments.done",
                    arguments=item["arguments"],
                    name=item["name"],
                    **where,
                )
            emit("response.output_item.done", output_index=index, item=item)

        if step.end == "cut":
            return
        if step.end == "failed":
            response = self._response(n, body, "failed", items, None)
            response["error"] = {"code": "server_error", "message": "upstream boom sk-upstream-secret"}
            emit("response.failed", response=response)
        elif step.end == "incomplete":
            response = self._response(n, body, "incomplete", items, _step_usage(step, n))
            response["incomplete_details"] = {"reason": "max_output_tokens"}
            emit("response.incomplete", response=response)
        else:
            emit("response.completed", response=self._response(n, body, "completed", items, _step_usage(step, n)))

    def _chat(self, handler, body, step, n):
        self._start_sse(handler)

        def chunk(delta, finish=None):
            payload = {
                "id": f"chatcmpl-{n}",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            handler.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            handler.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        chunk({"content": step.text if step.text is not None else f"echo: {last_user_text(body)}"})
        if step.end == "cut":
            return
        chunk({}, finish=step.end if step.end in ("length", "content_filter") else "stop")
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
