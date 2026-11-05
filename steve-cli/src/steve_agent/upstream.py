"""Adjusts Responses streams from llama.cpp, before LangChain parses them.

llama.cpp behind a gateway sends `output_index` and `content_index` as null on every stream event.
LangChain merges content blocks by those indexes, so without them the message text and tool call
arguments all land on the first block. Tokens still stream, but the final message has no text:
subagent results come back empty and later turns lose earlier replies.

It also sends reasoning as `reasoning_text` deltas, which LangChain ignores. They are rewritten as
reasoning summary deltas, the form LangChain keeps. ReplaySafeChatOpenAI turns them back on replay.
"""

import json

import httpx2
import openai


_RAW_REASONING = "response.reasoning_text.delta"
_SUMMARY_REASONING = "response.reasoning_summary_text.delta"


class _StreamAdjuster:
    def __init__(self):
        self._buffer = b""
        self._last_index = -1
        self._current = -1
        self._by_item: dict[str, int] = {}

    def feed(self, data: bytes) -> bytes:
        lines = (self._buffer + data).splitlines(keepends=True)
        self._buffer = lines.pop() if lines and not lines[-1].endswith((b"\n", b"\r")) else b""
        return b"".join(self._fill(line) for line in lines)

    def flush(self) -> bytes:
        remainder, self._buffer = self._buffer, b""
        return self._fill(remainder) if remainder else b""

    def _fill(self, line: bytes) -> bytes:
        if line.startswith(b"event:"):
            return line.replace(_RAW_REASONING.encode(), _SUMMARY_REASONING.encode())
        if not line.startswith(b"data:"):
            return line
        try:
            event = json.loads(line[5:])
        except ValueError:
            return line
        if not isinstance(event, dict) or not self._fill_event(event):
            return line
        ending = line[len(line.rstrip(b"\r\n")) :]
        return b"data: " + json.dumps(event).encode() + ending

    def _fill_event(self, event: dict) -> bool:
        kind = event.get("type")
        if not isinstance(kind, str) or not kind.startswith("response."):
            return False
        item = event.get("item")
        item_id = event.get("item_id") or (item.get("id") if isinstance(item, dict) else None)
        changed = False

        if kind == _RAW_REASONING:
            kind = event["type"] = _SUMMARY_REASONING
            event.setdefault("summary_index", 0)
            changed = True
        if event.get("output_index") is None and (item_id or item):
            if kind == "response.output_item.added":
                self._last_index += 1
                self._current = self._last_index
                if item_id:
                    self._by_item[item_id] = self._current
            event["output_index"] = self._by_item.get(item_id, self._current)
            changed = True
        if event.get("content_index") is None and any(
            f".{part}." in kind for part in ("output_text", "content_part", "refusal")
        ):
            event["content_index"] = 0
            changed = True
        return changed


class _AdjustedStream(httpx2.SyncByteStream, httpx2.AsyncByteStream):
    def __init__(self, stream):
        self._stream = stream
        self._adjuster = _StreamAdjuster()

    def __iter__(self):
        for chunk in self._stream:
            yield self._adjuster.feed(chunk)
        yield self._adjuster.flush()

    async def __aiter__(self):
        async for chunk in self._stream:
            yield self._adjuster.feed(chunk)
        yield self._adjuster.flush()

    def close(self):
        self._stream.close()

    async def aclose(self):
        await self._stream.aclose()


def _wrap_event_stream(response: httpx2.Response) -> None:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        response.stream = _AdjustedStream(response.stream)


async def _wrap_event_stream_async(response: httpx2.Response) -> None:
    _wrap_event_stream(response)


def http_clients() -> dict:
    """ChatOpenAI client options. Event hooks keep the SDK's defaults, including proxy settings."""
    return {
        "http_client": openai.DefaultHttpxClient(event_hooks={"response": [_wrap_event_stream]}),
        "http_async_client": openai.DefaultAsyncHttpxClient(
            event_hooks={"response": [_wrap_event_stream_async]}
        ),
    }
