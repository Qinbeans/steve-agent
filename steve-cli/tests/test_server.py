"""Contract tests: openai SDK or raw sockets -> uvicorn -> app -> real deep agent -> ChatOpenAI -> fake upstream."""

import json
import socket
import threading
import time

import httpx
import openai
import pytest
from fake_upstream import Step


def wait_until(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def raw_post(server, payload, headers=None):
    return httpx.post(f"{server.url}/v1/responses", json=payload, headers=headers or {})


def assert_error(response, status, code=None):
    assert response.status_code == status, response.text
    assert response.headers["x-should-retry"] == "false"
    error = response.json()["error"]
    assert set(error) == {"message", "type", "param", "code"}
    assert error["message"]
    if code:
        assert error["code"] == code
    return error


def sse_events(lines):
    """Parse complete SSE text into (event, data) pairs, checking the two lines agree."""
    events = []
    for block in lines.split("\n\n"):
        if not block or block.startswith(":"):
            continue
        event_line, data_line = block.split("\n")
        data = json.loads(data_line.removeprefix("data: "))
        assert event_line == f"event: {data['type']}"
        events.append(data)
    return events


def test_non_streaming_text(server, upstream):
    response = server.client.responses.create(input="hello", model="my-model", metadata={"k": "v"})

    assert response.status == "completed"
    assert response.output_text == "echo: hello"
    assert response.id.startswith("resp_")
    assert response.model == "my-model"
    assert response.metadata == {"k": "v"}
    assert response.usage.input_tokens == 100
    assert response.usage.input_tokens_details.cached_tokens == 3
    assert response.usage.output_tokens_details.reasoning_tokens == 2
    assert response.output[0].id.startswith("msg_")
    assert response.output[0].id != "msg_1"
    assert len(upstream.requests) == 1


def test_sdk_stream_matches_non_stream(server, upstream):
    steps = [Step(text="", reasoning=True, tools=[("ls", '{"path": "/"}')]), Step(text="All done.")]

    upstream.steps.extend(steps)
    plain = server.client.responses.create(input="go")

    upstream.steps.extend(steps)
    with server.client.responses.stream(input="go", model="m") as stream:
        events = list(stream)
        final = stream.get_final_response()

    def shape(response):
        return [
            (item.type, item.status, getattr(item, "name", None), getattr(item, "arguments", None))
            for item in response.output
        ]

    assert final.status == plain.status == "completed"
    assert final.output_text == plain.output_text == "All done."
    assert shape(final) == shape(plain)
    assert [item.type for item in final.output] == ["reasoning", "mcp_call", "message"]
    # Summed over both model calls; the fake charges 100 * n input tokens on the nth call.
    assert plain.usage.total_tokens == 100 + 200 + 40
    assert final.usage.total_tokens == 300 + 400 + 40
    assert [e.type for e in events[:2]] == ["response.created", "response.in_progress"]
    assert events[-1].type == "response.completed"
    sequence = [e.sequence_number for e in events if hasattr(e, "sequence_number")]
    assert sequence == sorted(set(sequence))


def test_tools_are_mcp_calls_and_never_client_tool_calls(server, upstream, steve_home):
    upstream.steps.extend(
        [
            Step(text="Looking.", tools=[("ls", '{"path": "/"}'), ("read_file", '{"file_path": "/missing"}')]),
            Step(text="Done."),
        ]
    )

    response = server.client.responses.create(input="go")

    types = [item.type for item in response.output]
    assert types == ["message", "mcp_call", "mcp_call", "message"]
    ls, missing = response.output[1:3]
    assert ls.server_label == "steve"
    assert ls.status == "completed" and "AGENTS.md" in ls.output
    assert missing.status == "failed" and missing.error is not None


def test_sse_wire_format(server):
    with httpx.stream("POST", f"{server.url}/v1/responses", json={"input": "hi", "stream": True}) as response:
        body = response.read().decode()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert "[DONE]" not in body
    events = sse_events(body)
    assert events[0]["type"] == "response.created"
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "echo: hi"
    assert {event["response"]["id"] for event in events if "response" in event} == {events[0]["response"]["id"]}


def test_stream_flag_decides_format_not_accept_header(server):
    response = raw_post(server, {"input": "hi", "stream": False}, {"accept": "text/event-stream"})
    assert response.headers["content-type"] == "application/json"

    with httpx.stream(
        "POST",
        f"{server.url}/v1/responses",
        json={"input": "hi", "stream": True},
        headers={"accept": "application/json"},
    ) as streamed:
        streamed.read()
    assert streamed.headers["content-type"].startswith("text/event-stream")


def test_events_arrive_while_the_upstream_is_still_running(server, upstream):
    upstream.steps.append(Step(text="hello world", hold=True))

    with httpx.stream("POST", f"{server.url}/v1/responses", json={"input": "x", "stream": True}) as response:
        lines = response.iter_lines()
        seen = []
        for line in lines:
            seen.append(line)
            if "response.output_text.delta" in line and line.startswith("data:"):
                break
        assert upstream.holding.is_set() and not upstream.release.is_set()
        upstream.release.set()
        rest = "\n".join(lines)
    assert "response.completed" in rest


def test_heartbeat_while_the_agent_is_quiet(start_server, upstream):
    server = start_server(heartbeat_seconds=0.05)
    upstream.steps.append(Step(text="hello world", hold=True))

    with httpx.stream("POST", f"{server.url}/v1/responses", json={"input": "x", "stream": True}) as response:
        lines = response.iter_lines()
        for line in lines:
            if line.startswith("event: response.output_text.delta"):
                break
        pings = 0
        for line in lines:
            if line == ": ping":
                pings += 1
                if pings == 2:
                    break
        upstream.release.set()
        rest = "\n".join(lines)
    assert pings == 2
    assert "response.completed" in rest


def test_sdk_tolerates_heartbeats(start_server, upstream):
    server = start_server(heartbeat_seconds=0.02)
    upstream.steps.append(Step(text="hello world", hold=True))
    threading.Timer(0.3, upstream.release.set).start()

    with server.client.responses.stream(input="x", model="m") as stream:
        final = stream.get_final_response()

    assert final.output_text == "hello world"


def test_stream_disconnect_cancels_the_run(server, upstream):
    upstream.steps.append(Step(text="hello world", hold=True))

    with httpx.stream("POST", f"{server.url}/v1/responses", json={"input": "x", "stream": True}) as response:
        for line in response.iter_lines():
            if line.startswith("event: response.output_text.delta"):
                break
        assert len(server.app.state.runs) == 1

    assert upstream.hung_up.wait(10), "upstream call kept running after the client left"
    wait_until(lambda: not server.app.state.runs)


def test_non_stream_disconnect_cancels_the_run(server, upstream):
    upstream.steps.append(Step(text="hello world", hold=True))
    body = json.dumps({"input": "x"}).encode()
    host, port = server.host_port

    with socket.create_connection((host, port)) as sock:
        sock.sendall(
            b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        assert upstream.holding.wait(10)
        assert len(server.app.state.runs) == 1

    assert upstream.hung_up.wait(10), "upstream call kept running after the client left"
    wait_until(lambda: not server.app.state.runs)


def test_disconnect_right_after_sending_cancels_the_run(server, upstream):
    """A client that leaves before any byte comes back must still stop the run."""
    upstream.steps.append(Step(text="hello world", hold=True))
    body = json.dumps({"input": "x", "stream": True}).encode()

    with socket.create_connection(server.host_port) as sock:
        sock.sendall(
            b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )

    wait_until(lambda: not server.app.state.runs)
    assert not upstream.holding.is_set() or upstream.hung_up.wait(10)


def test_finished_keep_alive_connection_does_not_cancel_later_runs(server):
    with httpx.Client(base_url=server.url) as http:
        first = http.post("/v1/responses", json={"input": "one"})
        second = http.post("/v1/responses", json={"input": "two"})

    assert first.json()["output"][0]["content"][0]["text"] == "echo: one"
    assert second.json()["output"][0]["content"][0]["text"] == "echo: two"
    assert not server.app.state.runs


def test_upstream_error_is_generic_with_no_secrets(server, upstream):
    upstream.steps.append(Step(http_error=(400, "bad key sk-upstream-secret at http://internal.example")))

    response = raw_post(server, {"input": "x"})

    assert_error(response, 500, "server_error")
    assert "sk-upstream-secret" not in response.text
    assert "internal.example" not in response.text
    assert upstream.base_url not in response.text


def test_upstream_failure_mid_stream_is_response_failed_with_partial_output(server, upstream):
    upstream.steps.extend([Step(text="partial", end="failed")])

    with httpx.stream("POST", f"{server.url}/v1/responses", json={"input": "x", "stream": True}) as response:
        body = response.read().decode()

    assert response.status_code == 200
    events = sse_events(body)
    terminal = events[-1]
    assert terminal["type"] == "response.failed"
    assert terminal["response"]["id"] == events[0]["response"]["id"]
    assert terminal["response"]["status"] == "failed"
    assert terminal["response"]["error"]["code"] == "server_error"
    assert terminal["response"]["output"][0]["content"][0]["text"] == "partial"
    assert "sk-upstream-secret" not in body
    assert "boom" not in body


def test_upstream_failure_non_stream_is_500_and_is_not_retried(start_server, upstream):
    server = start_server(max_retries=2)
    upstream.steps.append(Step(end="failed"))

    with pytest.raises(openai.InternalServerError) as caught:
        server.client.responses.create(input="x")

    assert caught.value.code == "server_error"
    assert "boom" not in str(caught.value)
    assert len(upstream.requests) == 1


def test_truncated_upstream_stream_fails(server, upstream):
    upstream.steps.append(Step(end="cut"))
    assert_error(raw_post(server, {"input": "x"}), 500, "upstream_stream_truncated")

    upstream.steps.append(Step(end="cut"))
    with httpx.stream("POST", f"{server.url}/v1/responses", json={"input": "x", "stream": True}) as response:
        terminal = sse_events(response.read().decode())[-1]
    assert terminal["type"] == "response.failed"
    assert terminal["response"]["error"]["code"] == "upstream_stream_truncated"


def test_incomplete_upstream_is_incomplete_in_both_modes(server, upstream):
    upstream.steps.append(Step(end="incomplete"))
    plain = raw_post(server, {"input": "x"})
    assert plain.status_code == 200
    assert plain.json()["status"] == "incomplete"
    assert plain.json()["incomplete_details"] == {"reason": "max_output_tokens"}

    upstream.steps.append(Step(end="incomplete"))
    with httpx.stream("POST", f"{server.url}/v1/responses", json={"input": "x", "stream": True}) as response:
        terminal = sse_events(response.read().decode())[-1]
    assert terminal["type"] == "response.incomplete"


def test_chat_completions_upstream_has_null_usage_and_is_not_truncated(start_server, upstream):
    server = start_server(use_responses_api=False)

    response = server.client.responses.create(input="hello")

    assert upstream.requests[0]["path"].endswith("/chat/completions")
    assert response.status == "completed"
    assert response.output_text == "echo: hello"
    assert response.usage is None


def test_instructions_reach_the_model_as_one_system_prompt(server, upstream):
    response = server.client.responses.create(
        input=[{"role": "developer", "content": "DEV_RULE"}, {"role": "user", "content": "hi"}],
        instructions="REQ_RULE",
    )

    assert response.instructions == "REQ_RULE"
    upstream_input = upstream.requests[0]["body"]["input"]
    systems = [item for item in upstream_input if item.get("role") == "system"]
    assert len(systems) == 1
    assert "REQ_RULE" in json.dumps(systems) and "DEV_RULE" in json.dumps(systems)
    assert "HOME_AGENTS_V1" in json.dumps(systems)


@pytest.mark.parametrize(
    ("payload", "param"),
    [
        ({"input": ""}, "input"),
        ({"input": "x", "tools": [{"type": "function"}]}, "tools"),
        ({"input": "x", "tool_choice": "required"}, "tool_choice"),
        ({"input": "x", "text": {"format": {"type": "json_object"}}}, "text.format"),
        ({"input": "x", "conversation": "c"}, "conversation"),
        ({"input": "x", "prompt": {"id": "p"}}, "prompt"),
        ({"input": "x", "background": True}, "background"),
        ({"input": [{"type": "function_call_output"}]}, "input[0].function_call_output"),
        ({"input": "x", "stream": "maybe"}, "stream"),
    ],
)
def test_rejected_requests_are_400_envelopes_and_never_run(server, upstream, payload, param):
    response = raw_post(server, payload)

    assert assert_error(response, 400)["param"] == param
    assert upstream.requests == []


def test_ignored_fields_are_accepted(server):
    response = raw_post(
        server,
        {"input": "x", "temperature": 0.1, "include": [], "reasoning": {"effort": "low"}, "unknown": 1, "tools": None},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "headers", [{"content-type": "text/plain"}, {"content-type": "application/x-www-form-urlencoded"}, {}]
)
def test_cross_site_simple_posts_are_blocked(server, upstream, headers):
    response = httpx.post(f"{server.url}/v1/responses", content='{"input": "x"}', headers=headers)

    assert_error(response, 400, "invalid_request")
    assert upstream.requests == []


def test_no_cors_preflight(server):
    response = httpx.options(
        f"{server.url}/v1/responses",
        headers={"origin": "http://evil.example", "access-control-request-method": "POST"},
    )
    assert_error(response, 405)
    assert "access-control-allow-origin" not in response.headers


def test_every_error_is_an_envelope(server):
    assert_error(httpx.get(f"{server.url}/nope"), 404)
    assert_error(httpx.get(f"{server.url}/v1/nope"), 404)
    assert_error(httpx.get(f"{server.url}/v1/responses"), 405)
    assert_error(httpx.post(f"{server.url}/v1/responses", content="{bad", headers={"content-type": "application/json"}), 400)


def test_docs_routes_are_off(server):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert httpx.get(f"{server.url}{path}").status_code == 404


def test_health_and_models(server):
    assert httpx.get(f"{server.url}/health").json() == {"status": "ok"}
    models = server.client.models.list()
    assert [model.id for model in models.data] == ["steve-agent"]


def test_auth(start_server, upstream):
    server = start_server(api_key="s3cret")
    url = server.url

    for request in (
        lambda **kw: httpx.post(f"{url}/v1/responses", json={"input": "x"}, **kw),
        lambda **kw: httpx.get(f"{url}/v1/models", **kw),
        lambda **kw: httpx.get(f"{url}/v1/unknown", **kw),
        lambda **kw: httpx.get(f"{url}/v1", **kw),
    ):
        assert_error(request(), 401, "invalid_api_key")
        assert_error(request(headers={"authorization": "Bearer wrong"}), 401)
        assert_error(request(headers={"authorization": "s3cret"}), 401)
        assert_error(request(headers={"authorization": "Bearer café".encode()}), 401)
    assert upstream.requests == []

    # Bad JSON from an unauthenticated client is still a 401: nothing parses before the key check.
    bad = httpx.post(f"{url}/v1/responses", content="{bad", headers={"content-type": "application/json"})
    assert_error(bad, 401)

    assert httpx.get(f"{url}/health").status_code == 200
    assert server.client.responses.create(input="x").output_text == "echo: x"
    assert server.client.models.list().data


def test_sdk_gets_authentication_error(start_server):
    server = start_server(api_key="s3cret")
    wrong = server.client.with_options(api_key="nope")
    with pytest.raises(openai.AuthenticationError) as caught:
        wrong.responses.create(input="x")
    assert caught.value.code == "invalid_api_key"


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "127.0.0.1.nip.io:8000", "evil.example:80"])
def test_host_allowlist_rejects(server, upstream, host):
    response = httpx.post(f"{server.url}/v1/responses", json={"input": "x"}, headers={"host": host})
    assert_error(response, 400, "invalid_host")
    assert_error(httpx.get(f"{server.url}/health", headers={"host": host}), 400, "invalid_host")
    assert upstream.requests == []


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:1234", "[::1]:8000", "LOCALHOST"])
def test_host_allowlist_accepts(server, host):
    assert httpx.get(f"{server.url}/health", headers={"host": host}).status_code == 200


def test_allowed_hosts_are_configurable(start_server):
    server = start_server(allowed_hosts=["steve.lan"])
    assert httpx.get(f"{server.url}/health", headers={"host": "steve.lan:8000"}).status_code == 200
    assert_error(httpx.get(f"{server.url}/health"), 400, "invalid_host")


def test_concurrent_runs_keep_their_own_tokens(server):
    results = {}

    def one(tag):
        results[tag] = server.client.responses.create(input=tag).output_text

    threads = [threading.Thread(target=one, args=(f"tag{i}",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == {f"tag{i}": f"echo: tag{i}" for i in range(6)}


def test_shutdown_cancels_running_runs(start_server, upstream):
    server = start_server()
    upstream.steps.append(Step(text="hello world", hold=True))
    done = []

    def post():
        try:
            done.append(raw_post(server, {"input": "x"}).status_code)
        except httpx.HTTPError as exc:
            done.append(type(exc))

    client = threading.Thread(target=post, daemon=True)
    client.start()
    assert upstream.holding.wait(10)
    server.uvicorn.should_exit = True  # What the SIGINT handler does; the fixture asserts it exits.

    assert upstream.hung_up.wait(10), "upstream call kept running after shutdown"
