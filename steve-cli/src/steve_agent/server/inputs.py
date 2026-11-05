import base64
import binascii
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from deepagents.backends.utils import create_file_data
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from steve_agent.server.errors import ApiError
from steve_agent.server.schemas import CreateResponseRequest


@dataclass(frozen=True)
class AgentInput:
    messages: list[BaseMessage]
    instructions: str | None
    files: dict[str, Any] = field(default_factory=dict)  # uploaded assets, as agent state files


def translate_input(request: CreateResponseRequest, max_asset_bytes: float = float("inf")) -> AgentInput:
    """Turn a request's input into agent messages and this request's instructions.

    System and developer messages go to the instructions, never into messages,
    because anything in messages persists in the thread's state.
    """
    items = request.input
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]

    messages: list[BaseMessage] = []
    files: dict[str, Any] = {}
    instructions = [request.instructions] if request.instructions else []
    for index, item in enumerate(items):
        path = f"input[{index}]"
        item_type = item.get("type") or "message"
        if item_type == "message":
            role = item.get("role")
            content = item.get("content")
            if role not in ("user", "assistant", "system", "developer"):
                raise _invalid(f"Unsupported message role: {role!r}.", f"{path}.role")
            if not content:
                raise ApiError(400, "Message content must not be empty.", param=f"{path}.content", code="empty_input")
            if role == "user":
                messages.append(HumanMessage(_user_content(content, path, files, max_asset_bytes)))
            elif role == "assistant":
                messages.append(AIMessage(_text(content, ("output_text", "input_text", "refusal"), path)))
            else:
                instructions.append(_text(content, ("input_text",), path))
        elif item_type == "mcp_call":
            messages.append(AIMessage(_tool_note(item)))
        elif item_type != "reasoning":
            raise _unsupported(f"Input item type {item_type!r} is not supported.", f"{path}.{item_type}")

    if not messages:
        raise ApiError(400, "'input' has no messages to run.", param="input", code="empty_input")
    return AgentInput(messages, "\n\n".join(filter(None, instructions)) or None, files)


def _user_content(content: Any, path: str, files: dict[str, Any], max_asset_bytes: float) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    blocks = []
    for part_path, part in _parts(content, path):
        match part.get("type"):
            case "input_text":
                blocks.append({"type": "text", "text": _part_text(part, part_path)})
            case "input_image":
                blocks.append(_image_block(part, part_path))
            case "input_file":
                blocks.append(_file_block(part, part_path, files, max_asset_bytes))
            case other:
                raise _unsupported(f"Content part type {other!r} is not supported.", f"{part_path}.{other}")
    return blocks


def _text(content: Any, allowed: tuple[str, ...], path: str) -> str:
    if isinstance(content, str):
        return content
    texts = []
    for part_path, part in _parts(content, path):
        part_type = part.get("type")
        if part_type not in allowed:
            raise _unsupported(
                f"Content part type {part_type!r} is not supported here; only text is.", f"{part_path}.{part_type}"
            )
        texts.append(_part_text(part, part_path, "refusal" if part_type == "refusal" else "text"))
    return "".join(texts)


def _parts(content: Any, path: str) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(content, list):
        raise _invalid("Message content must be a string or a list of parts.", f"{path}.content")
    parts = []
    for index, part in enumerate(content):
        part_path = f"{path}.content[{index}]"
        if not isinstance(part, dict):
            raise _invalid("A content part must be an object.", part_path)
        parts.append((part_path, part))
    return parts


def _part_text(part: dict[str, Any], path: str, field: str = "text") -> str:
    text = part.get(field)
    if not isinstance(text, str):
        raise _invalid(f"This part needs a string '{field}'.", f"{path}.{field}")
    return text


def _image_block(part: dict[str, Any], path: str) -> dict[str, Any]:
    url = part.get("image_url")
    if not url or not isinstance(url, str):
        raise _invalid("An input_image needs an 'image_url'; 'file_id' is not supported.", f"{path}.image_url")
    image = {"url": url}
    if part.get("detail"):
        image["detail"] = part["detail"]
    return {"type": "image_url", "image_url": image}


def _file_block(part: dict[str, Any], path: str, files: dict[str, Any], max_asset_bytes: float) -> dict[str, Any]:
    """Save an uploaded file in the conversation's state and tell the agent where it is.

    The agent reads it with its file tools, which works whatever the model accepts directly. Being
    in state, the file forks and is deleted with the conversation, and counts towards its memory cap.
    """
    data = part.get("file_data")
    if not data or not isinstance(data, str):
        raise _invalid("An input_file needs inline 'file_data'; 'file_id' and 'file_url' are not supported.", f"{path}.file_data")
    media_type = None
    if data.startswith("data:"):
        header, _, data = data.partition(",")
        media_type = header[5:].split(";")[0] or None
    if len(data) * 3 / 4 > max_asset_bytes:
        raise ApiError(413, f"This file is over the {max_asset_bytes / 1024 / 1024:g} MB limit.", param=f"{path}.file_data", code="asset_too_large")
    try:
        content = base64.b64decode(data, validate=True)
    except binascii.Error:
        raise _invalid("'file_data' is not valid base64.", f"{path}.file_data") from None

    # Only ever a bare name: this becomes part of a path.
    name = re.sub(r"[^\w.\- ]", "_", PurePosixPath(str(part.get("filename") or "file").replace("\\", "/")).name).strip(". ") or "file"
    key = f"/uploads/{uuid4().hex[:8]}/{name}"
    try:
        files[key] = create_file_data(content.decode("utf-8"), encoding="utf-8")
    except UnicodeDecodeError:
        files[key] = create_file_data(base64.b64encode(content).decode("ascii"), encoding="base64")
    media_type = media_type or mimetypes.guess_type(name)[0] or "unknown type"
    return {
        "type": "text",
        "text": f"[Attached file: {name} ({media_type}, {len(content)} bytes), at /artifacts{key}. Read it with read_file.]",
    }


def _tool_note(item: dict[str, Any]) -> str:
    result = item.get("error") or item.get("output")
    return f"[Earlier tool call] {item.get('name')} with arguments {item.get('arguments')} returned: {result}"


def _invalid(message: str, param: str) -> ApiError:
    return ApiError(400, message, param=param, code="invalid_input")


def _unsupported(message: str, param: str) -> ApiError:
    return ApiError(400, message, param=param, code="unsupported_input_item")
