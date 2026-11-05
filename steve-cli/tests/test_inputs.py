import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from steve_agent.server.errors import ApiError
from steve_agent.server.inputs import translate_input
from steve_agent.server.schemas import CreateResponseRequest


def translate(input, **fields):
    return translate_input(CreateResponseRequest(input=input, **fields))


def user(content):
    return {"role": "user", "content": content}


def rejection(fn, *args, **kwargs):
    with pytest.raises(ApiError) as caught:
        fn(*args, **kwargs)
    assert caught.value.status == 400
    return caught.value


# Request fields


def test_defaults():
    request = CreateResponseRequest(input="hi")
    assert request.model == "steve-agent"
    assert request.stream is False
    assert request.store is True
    assert request.instructions is None
    assert request.metadata is None
    assert request.previous_response_id is None


def test_honoured_fields_are_kept():
    request = CreateResponseRequest(
        input="hi",
        stream=True,
        instructions="be brief",
        metadata={"k": "v"},
        model="gpt-whatever",
        previous_response_id="resp_1",
        store=False,
    )
    assert request.stream is True
    assert request.instructions == "be brief"
    assert request.metadata == {"k": "v"}
    assert request.model == "gpt-whatever"
    assert request.previous_response_id == "resp_1"
    assert request.store is False


@pytest.mark.parametrize(
    "fields",
    [
        {"tools": [{"type": "function", "name": "x"}]},
        {"tool_choice": "required"},
        {"tool_choice": {"type": "function", "name": "x"}},
        {"text": {"format": {"type": "json_schema", "name": "x", "schema": {}}}},
        {"text": {"format": {"type": "json_object"}}},
        {"conversation": "conv_1"},
        {"conversation": {"id": "conv_1"}},
        {"prompt": {"id": "pmpt_1"}},
        {"background": True},
    ],
)
def test_unsupported_fields_are_rejected(fields):
    error = rejection(CreateResponseRequest, input="hi", **fields)
    name = next(iter(fields))
    assert error.param == ("text.format" if name == "text" else name)
    assert error.code == "unsupported_parameter"


@pytest.mark.parametrize(
    "fields",
    [
        {"tools": []},
        {"tool_choice": "auto"},
        {"tool_choice": "none"},
        {"text": {"format": {"type": "text"}}},
        {"text": {"verbosity": "low"}},
        {"background": False},
    ],
)
def test_harmless_values_of_rejected_fields_are_accepted(fields):
    assert translate("hi", **fields).messages == [HumanMessage("hi")]


def test_explicit_nulls_mean_absent():
    request = CreateResponseRequest.model_validate(
        {
            "input": "hi",
            "model": None,
            "stream": None,
            "store": None,
            "tools": None,
            "tool_choice": None,
            "text": None,
            "conversation": None,
            "prompt": None,
            "background": None,
        }
    )
    assert request.model == "steve-agent"
    assert request.stream is False
    assert request.store is True


def test_ignored_fields_are_logged_by_name_at_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger="steve_agent.server.schemas"):
        request = CreateResponseRequest.model_validate(
            {
                "input": "hi",
                "temperature": "not even a number",
                "reasoning": {"effort": "high"},
                "max_output_tokens": 5,
                "brand_new_field": "secret-value",
            }
        )
    assert not hasattr(request, "temperature")
    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
    assert "brand_new_field, max_output_tokens, reasoning, temperature" in caplog.text
    assert "secret-value" not in caplog.text


@pytest.mark.parametrize("body", [{}, {"input": None}, {"input": ""}, {"input": []}])
def test_empty_input_is_rejected(body):
    error = rejection(CreateResponseRequest.model_validate, body)
    assert error.param == "input"
    assert error.code == "empty_input"


def test_wrong_field_type_stays_a_validation_error():
    with pytest.raises(ValidationError):
        CreateResponseRequest(input=["not an item"])


def test_rejection_reaches_the_app_exception_handler():
    """The server relies on ApiError escaping FastAPI's body parsing unconverted."""
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def handle(request, error):
        return JSONResponse({"param": error.param}, status_code=error.status)

    @app.post("/echo")
    async def echo(body: CreateResponseRequest):
        return {"model": body.model}

    client = TestClient(app)
    assert client.post("/echo", json={"input": "hi"}).json() == {"model": "steve-agent"}
    response = client.post("/echo", json={"input": "hi", "tools": [{"type": "function"}]})
    assert response.status_code == 400
    assert response.json() == {"param": "tools"}


# Accepted input shapes


def test_string_input():
    result = translate("hello")
    assert result.messages == [HumanMessage("hello")]
    assert result.instructions is None


def test_user_message_with_string_content_and_no_type():
    assert translate([user("hello")]).messages == [HumanMessage("hello")]


def test_user_message_with_explicit_type():
    assert translate([{"type": "message", **user("hello")}]).messages == [HumanMessage("hello")]


def test_user_text_part():
    message = translate([user([{"type": "input_text", "text": "hello"}])]).messages[0]
    assert isinstance(message, HumanMessage)
    assert message.content == [{"type": "text", "text": "hello"}]


def test_user_image_parts():
    message = translate(
        [
            user(
                [
                    {"type": "input_text", "text": "look"},
                    {"type": "input_image", "image_url": "https://x.test/a.png"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AA==", "detail": "low"},
                    {"type": "input_image", "image_url": "https://x.test/b.png", "detail": None},
                ]
            )
        ]
    ).messages[0]
    assert message.content == [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA==", "detail": "low"}},
        {"type": "image_url", "image_url": {"url": "https://x.test/b.png"}},
    ]


def test_assistant_message_with_string_content():
    result = translate([user("hi"), {"role": "assistant", "content": "hello"}])
    assert result.messages == [HumanMessage("hi"), AIMessage("hello")]


def test_assistant_replay_is_text_only_and_drops_our_ids():
    message = translate(
        [
            {
                "id": "msg_ours",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "one ", "annotations": [], "logprobs": []},
                    {"type": "output_text", "text": "two", "annotations": []},
                ],
            }
        ]
    ).messages[0]
    assert isinstance(message, AIMessage)
    assert message.content == "one two"
    assert message.id is None
    assert message.response_metadata == {}
    assert message.additional_kwargs == {}


def test_assistant_input_text_part_is_accepted():
    message = translate([{"role": "assistant", "content": [{"type": "input_text", "text": "x"}]}]).messages[0]
    assert message.content == "x"


@pytest.mark.parametrize("role", ["system", "developer"])
def test_system_and_developer_messages_become_instructions(role):
    result = translate([{"role": role, "content": "be brief"}, user("hi")])
    assert result.messages == [HumanMessage("hi")]
    assert result.instructions == "be brief"


def test_instructions_merge_in_order_and_mid_history_stays_out_of_messages():
    result = translate(
        [
            {"role": "system", "content": "first"},
            user("hi"),
            {"role": "developer", "content": [{"type": "input_text", "text": "se"}, {"type": "input_text", "text": "cond"}]},
            {"role": "assistant", "content": "hello"},
            user("again"),
        ],
        instructions="base",
    )
    assert result.instructions == "base\n\nfirst\n\nsecond"
    assert result.messages == [HumanMessage("hi"), AIMessage("hello"), HumanMessage("again")]


def test_request_instructions_alone_pass_through():
    assert translate("hi", instructions="be brief").instructions == "be brief"


def test_reasoning_is_dropped():
    result = translate(
        [
            user("hi"),
            {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque"},
            {"role": "assistant", "content": "hello"},
        ]
    )
    assert result.messages == [HumanMessage("hi"), AIMessage("hello")]


def test_mcp_call_becomes_a_note():
    message = translate(
        [
            {
                "id": "mcp_ours",
                "type": "mcp_call",
                "server_label": "steve",
                "name": "ls",
                "arguments": '{"path": "/"}',
                "output": "a.txt",
                "status": "completed",
            }
        ]
    ).messages[0]
    assert isinstance(message, AIMessage)
    assert message.id is None
    assert message.tool_calls == []
    assert "ls" in message.content
    assert '{"path": "/"}' in message.content
    assert "a.txt" in message.content
    assert "mcp_ours" not in message.content


def test_failed_mcp_call_note_carries_the_error():
    message = translate(
        [
            {
                "type": "mcp_call",
                "name": "ls",
                "arguments": "{}",
                "output": None,
                "error": {"type": "mcp_tool_execution_error", "content": [{"type": "text", "text": "boom"}]},
                "status": "failed",
            }
        ]
    ).messages[0]
    assert "boom" in message.content


def test_mcp_call_without_a_result_is_accepted():
    message = translate([{"type": "mcp_call", "name": "ls", "arguments": "{}", "status": "incomplete"}]).messages[0]
    assert "ls" in message.content


# Input rejections


@pytest.mark.parametrize(
    "item_type",
    [
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "item_reference",
        "computer_call",
    ],
)
def test_unsupported_item_types_name_the_type_in_param(item_type):
    error = rejection(translate, [user("hi"), {"type": item_type, "id": "x"}])
    assert error.param == f"input[1].{item_type}"
    assert error.code == "unsupported_input_item"
    assert item_type in error.message


def test_input_file_without_inline_data_is_rejected():
    error = rejection(translate, [user([{"type": "input_text", "text": "x"}, {"type": "input_file", "file_id": "f"}])])
    assert error.param == "input[0].content[1].file_data"
    assert error.code == "invalid_input"


@pytest.mark.parametrize("role", ["assistant", "system", "developer"])
def test_images_are_rejected_outside_user_messages(role):
    part = {"type": "input_image", "image_url": "https://x.test/a.png"}
    error = rejection(translate, [user("hi"), {"role": role, "content": [part]}])
    assert error.param == "input[1].content[0].input_image"


def test_assistant_refusal_part_replays_as_text():
    messages = translate([{"role": "assistant", "content": [{"type": "refusal", "refusal": "no"}]}, user("hi")]).messages
    assert isinstance(messages[0], AIMessage)
    assert messages[0].content == "no"


def test_refusal_part_is_rejected_outside_assistant_messages():
    error = rejection(translate, [{"role": "system", "content": [{"type": "refusal", "refusal": "no"}]}, user("hi")])
    assert error.param == "input[0].content[0].refusal"


def test_image_without_url_is_rejected():
    error = rejection(translate, [user([{"type": "input_image", "file_id": "file_1"}])])
    assert error.param == "input[0].content[0].image_url"
    assert error.code == "invalid_input"


@pytest.mark.parametrize("role", [None, "tool", "critic"])
def test_unknown_role_is_rejected(role):
    error = rejection(translate, [{"role": role, "content": "x"}])
    assert error.param == "input[0].role"


@pytest.mark.parametrize("content", [None, "", []])
def test_empty_message_content_is_rejected(content):
    error = rejection(translate, [user("hi"), user(content)])
    assert error.param == "input[1].content"
    assert error.code == "empty_input"


@pytest.mark.parametrize(
    "content, param",
    [
        (5, "input[0].content"),
        (["text"], "input[0].content[0]"),
        ([{"type": "input_text"}], "input[0].content[0].text"),
        ([{"type": "input_text", "text": 5}], "input[0].content[0].text"),
    ],
)
def test_malformed_content_is_rejected(content, param):
    error = rejection(translate, [user(content)])
    assert error.param == param
    assert error.code == "invalid_input"


@pytest.mark.parametrize(
    "items",
    [
        [{"type": "reasoning", "summary": []}],
        [{"role": "system", "content": "only instructions"}],
    ],
)
def test_input_with_nothing_to_run_is_rejected(items):
    error = rejection(translate, items)
    assert error.param == "input"
    assert error.code == "empty_input"
