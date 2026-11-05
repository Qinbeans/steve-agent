"""Client contract: real openai SDK and ChatOpenAI -> uvicorn -> app -> real deep agent -> fake upstream.

SDK clients keep their default retries, so a retried request shows up as extra upstream calls.
"""

import json
import re

import openai
import pytest
from fake_upstream import Step
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from openai.types.responses import Response, ResponseOutputMessage, ResponseReasoningItem
from openai.types.responses.response_input_message_item import ResponseInputMessageItem
from openai.types.responses.response_output_item import McpCall

TOOL_RUN = [
    Step(text="", reasoning=True, tools=[("ls", '{"path": "/"}')]),
    Step(
        text="Now reading.",
        tools=[("read_file", '{"file_path": "/AGENTS.md"}'), ("read_file", '{"file_path": "/nope"}')],
    ),
    Step(text="All done."),
]


@pytest.fixture
def client(server):
    sdk = openai.OpenAI(base_url=f"{server.url}/v1", api_key="unused")
    assert sdk.max_retries == openai.DEFAULT_MAX_RETRIES > 0
    return sdk


def scripted(upstream, steps):
    """Queue steps on an upstream that looks unused, so usage and ids come out the same each time."""
    upstream._n = 0
    upstream.requests.clear()
    upstream.steps.extend(steps)


def comparable_items(response):
    """Output items as plain data, without the ids we mint per run."""
    return [{k: v for k, v in item.items() if k != "id"} for item in items_with_ids(response)]


def items_with_ids(response):
    items = [item.model_dump(warnings=False) for item in response.output]
    # The stream helper returns a ParsedResponse, which adds a client-side "parsed" key to text parts.
    for item in items:
        for part in item.get("content") or []:
            part.pop("parsed", None)
    return items


def reassemble(events):
    """Rebuild each output item from the stream's added/delta/result events alone, ignoring the done snapshots."""
    items = {}
    for event in events:
        match event.type:
            case "response.output_item.added":
                assert event.output_index not in items
                name = getattr(event.item, "name", None)
                items[event.output_index] = {"type": event.item.type, "name": name, "text": "", "summary": {}}
            case "response.output_text.delta":
                items[event.output_index]["text"] += event.delta
            case "response.reasoning_summary_text.delta":
                summary = items[event.output_index]["summary"]
                summary[event.summary_index] = summary.get(event.summary_index, "") + event.delta
            case "response.mcp_call_arguments.delta":
                items[event.output_index]["text"] += event.delta
    return [items[index] for index in sorted(items)]


def digest(response):
    """The same shape as reassemble(), from a finished response."""
    result = []
    for item in response.output:
        entry = {"type": item.type, "name": None, "text": "", "summary": {}}
        if isinstance(item, ResponseOutputMessage):
            entry["text"] = "".join(part.text for part in item.content)
        elif isinstance(item, ResponseReasoningItem):
            entry["summary"] = {index: part.text for index, part in enumerate(item.summary)}
        elif isinstance(item, McpCall):
            entry["name"], entry["text"] = item.name, item.arguments
        result.append(entry)
    return result


def input_types(request):
    body = request["body"]["input"]
    return [item.get("type", "message") for item in body]


# Create and streaming


def test_create_returns_a_typed_response(client, upstream):
    upstream.steps.append(Step(text="Hello there."))

    response = client.responses.create(input="hi", model="my-model", metadata={"k": "v"}, instructions="Be brief.")

    assert isinstance(response, Response)
    assert response.status == "completed"
    assert response.output_text == "Hello there."
    assert response.model == "my-model"
    assert response.metadata == {"k": "v"}
    assert response.instructions == "Be brief."
    assert response.error is None
    assert len(upstream.requests) == 1


def test_create_stream_reassembles_to_the_non_streaming_response(client, upstream):
    scripted(upstream, TOOL_RUN)
    plain = client.responses.create(input="go")

    scripted(upstream, TOOL_RUN)
    events = list(client.responses.create(input="go", stream=True))
    final = events[-1].response

    assert [event.type for event in events[:2]] == ["response.created", "response.in_progress"]
    assert events[-1].type == "response.completed"
    sequence = [event.sequence_number for event in events]
    assert sequence == sorted(set(sequence))

    assert reassemble(events) == digest(plain)
    assert comparable_items(final) == comparable_items(plain)
    # Parallel tools finish in any order, so done events are matched by output_index.
    done = {event.output_index: event.item for event in events if event.type == "response.output_item.done"}
    assert [done[index].model_dump(warnings=False) for index in sorted(done)] == [
        item.model_dump(warnings=False) for item in final.output
    ]
    assert final.usage == plain.usage
    assert final.status == plain.status == "completed"
    assert final.output_text == plain.output_text


def test_stream_helper_final_response_matches_non_streaming(client, upstream):
    scripted(upstream, TOOL_RUN)
    plain = client.responses.create(input="go")

    scripted(upstream, TOOL_RUN)
    with client.responses.stream(input="go", model="m") as stream:
        text = "".join(event.delta for event in stream if event.type == "response.output_text.delta")
        final = stream.get_final_response()

    assert final.id != plain.id
    assert comparable_items(final) == comparable_items(plain)
    assert final.output_text == plain.output_text == text == "Now reading.All done."
    assert final.usage == plain.usage


def test_stream_helper_sees_a_failed_run_as_failed(client, upstream):
    upstream.steps.append(Step(text="Partial answer", end="failed"))

    with client.responses.stream(input="go", model="m") as stream:
        events = list(stream)
        created = next(event for event in events if event.type == "response.created")

    assert events[-1].type == "response.failed"
    assert events[-1].response.id == created.response.id
    assert events[-1].response.output_text == "Partial answer"
    assert "sk-upstream-secret" not in events[-1].response.model_dump_json(warnings=False)
    assert len(upstream.requests) == 1


# Tools


def test_multi_step_tool_run_produces_mcp_calls_and_never_function_calls(client, upstream):
    upstream.steps.extend(TOOL_RUN)

    response = client.responses.create(input="go")

    assert [item.type for item in response.output] == [
        "reasoning",
        "mcp_call",
        "message",
        "mcp_call",
        "mcp_call",
        "message",
    ]
    listing, reading, missing = (item for item in response.output if item.type == "mcp_call")
    assert all(isinstance(item, McpCall) and item.server_label == "steve" for item in (listing, reading, missing))
    assert listing.name == "ls" and json.loads(listing.arguments) == {"path": "/"}
    assert listing.status == "completed" and "AGENTS.md" in listing.output
    assert reading.status == "completed" and "HOME_AGENTS_V1" in reading.output
    assert missing.status == "failed" and missing.error is not None

    # The agent ran the tools itself: each later model call carries the results.
    assert len(upstream.requests) == 3
    assert input_types(upstream.requests[1]).count("function_call_output") == 1
    assert input_types(upstream.requests[2]).count("function_call_output") == 3


def test_tool_run_never_mentions_client_tool_types_on_the_wire(client, upstream):
    scripted(upstream, TOOL_RUN)
    plain = client.responses.create(input="go")

    scripted(upstream, TOOL_RUN)
    events = list(client.responses.create(input="go", stream=True))

    for text in [plain.model_dump_json(warnings=False), *(e.model_dump_json(warnings=False) for e in events)]:
        assert "function_call" not in text
        assert "custom_tool_call" not in text
    added = [event.item.type for event in events if event.type == "response.output_item.added"]
    assert added.count("mcp_call") == 3


# State routes


def test_previous_response_id_carries_the_conversation(client, upstream):
    upstream.steps.extend([Step(text="The answer is 42."), Step(text="Yes, 42.")])

    first = client.responses.create(input="What is the answer?")
    second = client.responses.create(input="Are you sure?", previous_response_id=first.id)

    assert second.previous_response_id == first.id
    assert second.output_text == "Yes, 42."
    sent = upstream.body_text(-1)
    assert "What is the answer?" in sent and "The answer is 42." in sent and "Are you sure?" in sent


def test_previous_response_id_with_a_streamed_parent_and_child(client, upstream):
    upstream.steps.extend([Step(text="Parent text."), Step(text="Child text.")])

    with client.responses.stream(input="first", model="m") as stream:
        parent = stream.get_final_response()
    with client.responses.stream(input="second", model="m", previous_response_id=parent.id) as stream:
        child = stream.get_final_response()

    assert child.previous_response_id == parent.id
    assert child.output_text == "Child text."
    assert "Parent text." in upstream.body_text(-1)


def test_retrieve_returns_what_create_returned(client):
    created = client.responses.create(input="hi", metadata={"k": "v"})

    retrieved = client.responses.retrieve(created.id)

    assert isinstance(retrieved, Response)
    assert retrieved.model_dump(warnings=False) == created.model_dump(warnings=False)


def test_retrieve_returns_what_a_stream_ended_with(client):
    with client.responses.stream(input="hi", model="m") as stream:
        final = stream.get_final_response()

    retrieved = client.responses.retrieve(final.id)
    assert retrieved.status == final.status
    assert retrieved.usage == final.usage
    assert items_with_ids(retrieved) == items_with_ids(final)


def test_delete(client, upstream):
    response = client.responses.create(input="hi")

    assert client.responses.delete(response.id) is None

    with pytest.raises(openai.NotFoundError):
        client.responses.retrieve(response.id)
    with pytest.raises(openai.NotFoundError):
        client.responses.delete(response.id)
    calls = len(upstream.requests)
    with pytest.raises(openai.NotFoundError) as caught:
        client.responses.create(input="more", previous_response_id=response.id)
    assert caught.value.code == "previous_response_not_found"
    assert len(upstream.requests) == calls


def test_input_items_are_typed_and_page_with_the_sdk_cursor(client):
    response = client.responses.create(
        input=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": [{"type": "input_text", "text": "three"}]},
        ]
    )

    newest_first = client.responses.input_items.list(response.id)
    assert [item.role for item in newest_first.data] == ["user", "assistant", "user"]
    assert [item.content[0].text for item in newest_first.data] == ["three", "two", "one"]
    assert isinstance(newest_first.data[0], ResponseInputMessageItem)
    assert newest_first.has_more is False

    page = client.responses.input_items.list(response.id, limit=1, order="asc")
    assert page.has_more is True
    assert len(page.data) == 1
    walked = [item.id for item in page]  # Iterating a cursor page fetches the following pages.
    assert walked == [item.id for item in newest_first.data[::-1]]


# Errors. Each asserts the upstream count, because a retried request would add calls.


def test_400_is_bad_request_with_the_envelope_fields(client, upstream):
    tool = {"type": "function", "name": "f", "parameters": {}, "strict": False}

    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(input="hi", tools=[tool])

    assert caught.value.status_code == 400
    assert caught.value.type == "invalid_request_error"
    assert caught.value.code == "unsupported_parameter"
    assert caught.value.param == "tools"
    assert caught.value.response.headers["x-should-retry"] == "false"
    assert upstream.requests == []


def test_client_tool_item_in_input_is_rejected_by_name(client, upstream):
    item = {"type": "function_call_output", "call_id": "c", "output": "x"}

    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(input=[item])

    assert caught.value.param == "input[0].function_call_output"
    assert caught.value.code == "unsupported_input_item"
    assert upstream.requests == []


def test_401_is_authentication_error(start_server, upstream):
    server = start_server(api_key="right-key")
    client = openai.OpenAI(base_url=f"{server.url}/v1", api_key="wrong-key")

    with pytest.raises(openai.AuthenticationError) as caught:
        client.responses.create(input="hi")

    assert caught.value.status_code == 401
    assert caught.value.code == "invalid_api_key"
    assert upstream.requests == []

    assert server.client.responses.create(input="hi").status == "completed"
    assert len(upstream.requests) == 1


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.responses.retrieve("resp_missing"),
        lambda client: client.responses.delete("resp_missing"),
        lambda client: client.responses.input_items.list("resp_missing"),
    ],
    ids=["retrieve", "delete", "input_items"],
)
def test_404_is_not_found_error(client, call):
    with pytest.raises(openai.NotFoundError) as caught:
        call(client)

    assert caught.value.status_code == 404
    assert caught.value.code == "response_not_found"


def test_unknown_previous_response_is_not_found_error(client, upstream):
    with pytest.raises(openai.NotFoundError) as caught:
        client.responses.create(input="hi", previous_response_id="resp_missing")

    assert caught.value.code == "previous_response_not_found"
    assert caught.value.param == "previous_response_id"
    assert upstream.requests == []


@pytest.mark.parametrize(
    "call, param",
    [
        (lambda client: client.responses.create(input="hi", metadata={"k": 1}), "metadata.k"),
        (lambda client: client.responses.create(input=5), "input"),
        (lambda client: client.responses.create(input=[1]), "input"),
        (lambda client: client.responses.create(input="hi", extra_body={"stream": "nope"}), "stream"),
        (lambda client: client.responses.input_items.list("resp_x", limit=1000), "limit"),
    ],
    ids=["metadata", "input_number", "input_list_of_numbers", "stream", "query"],
)
def test_validation_errors_are_bad_request_not_422(client, upstream, call, param):
    with pytest.raises(openai.BadRequestError) as caught:
        call(client)

    assert caught.value.status_code == 400
    assert caught.value.type == "invalid_request_error"
    assert caught.value.param == param
    assert upstream.requests == []


def test_empty_input_is_bad_request(client, upstream):
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(input="")

    assert caught.value.code == "empty_input"
    assert upstream.requests == []


def test_sdk_does_not_retry_a_run_that_failed_with_500(client, upstream):
    # A 400 from upstream is not retried by the inner ChatOpenAI, so every extra call would be the outer SDK retrying.
    upstream.steps.extend(Step(http_error=(400, "boom sk-upstream-secret")) for _ in range(3))

    with pytest.raises(openai.InternalServerError) as caught:
        client.responses.create(input="hi")

    assert len(upstream.requests) == 1
    assert caught.value.status_code == 500
    assert caught.value.response.headers["x-should-retry"] == "false"
    assert "sk-upstream-secret" not in caught.value.message


def test_sdk_does_not_retry_a_failed_stream(client, upstream):
    upstream.steps.extend(Step(http_error=(400, "boom")) for _ in range(3))

    events = list(client.responses.create(input="hi", stream=True))

    assert events[-1].type == "response.failed"
    assert len(upstream.requests) == 1


# ChatOpenAI as a client of the adapter


def chat_model(server, **options):
    url = f"{server.url}/v1"
    return ChatOpenAI(model="steve-agent", base_url=url, api_key="unused", use_responses_api=True, **options)


def run(model, messages, streaming):
    if not streaming:
        return model.invoke(messages)
    merged = None
    for chunk in model.stream(messages):
        merged = chunk if merged is None else merged + chunk
    return merged


def block_types(message):
    return [block["type"] for block in message.content]


both_modes = pytest.mark.parametrize("streaming", [False, True], ids=["invoke", "stream"])


@both_modes
def test_chat_openai_gets_text(server, upstream, streaming):
    upstream.steps.append(Step(text="Hello from steve."))

    message = run(chat_model(server), "hi", streaming)

    assert message.text == "Hello from steve."
    assert message.tool_calls == []
    assert message.invalid_tool_calls == []


@both_modes
def test_chat_openai_returns_no_tool_calls_for_our_items(server, upstream, streaming):
    upstream.steps.extend(TOOL_RUN)

    message = run(chat_model(server), "go", streaming)

    assert message.tool_calls == []
    assert message.invalid_tool_calls == []
    if streaming:
        assert message.tool_call_chunks == []
    assert block_types(message).count("mcp_call") == 3
    assert "function_call" not in block_types(message)
    assert message.text == "Now reading.All done."
    assert message.response_metadata["status"] == "completed"
    assert len(upstream.requests) == 3


@both_modes
def test_chat_openai_replays_its_own_output(server, upstream, streaming):
    upstream.steps.extend([*TOOL_RUN, Step(text="Second turn.")])
    model = chat_model(server)

    first = run(model, "go", streaming)
    calls = len(upstream.requests)
    history = [HumanMessage("go"), first, HumanMessage("and then?")]
    second = run(model, history, streaming)

    assert second.text == "Second turn."
    assert second.tool_calls == []
    assert len(upstream.requests) == calls + 1
    sent = upstream.body_text(-1)
    assert "Now reading." in sent and "All done." in sent and "and then?" in sent
    assert "HOME_AGENTS_V1" in sent  # The tool output survives the replay, as the note made from the mcp_call.
    # Replayed items become fresh upstream history, not tool protocol the upstream never saw.
    assert "function_call" not in input_types(upstream.requests[-1])
    assert not re.search(r"(mcp|msg|rs)_[0-9a-f]{32}", json.dumps(upstream.requests[-1]["body"]["input"]))

    # The replayed mcp_call items are listed back in a shape the SDK can parse.
    client = openai.OpenAI(base_url=f"{server.url}/v1", api_key="unused")
    listed = client.responses.input_items.list(second.response_metadata["id"], order="asc").data
    assert [item.type for item in listed].count("mcp_call") == 3


def test_chat_openai_replays_reasoning_without_forwarding_it(server, upstream):
    upstream.steps.extend([Step(text="Thought about it.", reasoning=True), Step(text="Next.")])
    model = chat_model(server)

    first = model.invoke("go")
    assert block_types(first) == ["reasoning", "text"]
    second = model.invoke([HumanMessage("go"), first, HumanMessage("more")])

    assert second.text == "Next."
    assert "reasoning" not in input_types(upstream.requests[-1])
    assert "Thought about it." in upstream.body_text(-1)


def test_chat_openai_can_use_previous_response_id(server, upstream):
    upstream.steps.extend([Step(text="Remember 7."), Step(text="It was 7.")])
    model = chat_model(server, use_previous_response_id=True)

    first = model.invoke("Pick a number.")
    second = model.invoke([HumanMessage("Pick a number."), first, HumanMessage("Which one?")])

    assert second.text == "It was 7."
    assert "Remember 7." in upstream.body_text(-1)
    client = openai.OpenAI(base_url=f"{server.url}/v1", api_key="unused")
    stored = client.responses.retrieve(second.response_metadata["id"])
    assert stored.previous_response_id == first.response_metadata["id"]


def test_chat_openai_replay_of_a_plain_assistant_message(server, upstream):
    upstream.steps.append(Step(text="ok"))

    reply = chat_model(server).invoke([HumanMessage("a"), AIMessage("b"), HumanMessage("c")])

    assert reply.text == "ok"
    sent = upstream.body_text(-1)
    assert '"a"' in sent and '"b"' in sent and '"c"' in sent


def test_chat_openai_with_client_tools_gets_a_clear_rejection(server, upstream):
    def client_tool(path: str) -> str:
        """Pretend client tool."""
        return path

    model = chat_model(server).bind_tools([client_tool])

    with pytest.raises(openai.BadRequestError) as caught:
        model.invoke("hi")

    assert caught.value.param == "tools"
    assert upstream.requests == []
