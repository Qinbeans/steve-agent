import logging
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from steve_agent.server.errors import ApiError

logger = logging.getLogger(__name__)


from steve_agent.interaction import Mode


class CreateResponseRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input: str | list[dict[str, Any]] = ""
    stream: bool = False
    instructions: str | None = None
    metadata: dict[str, str] | None = None
    model: str = "steve-agent"
    previous_response_id: str | None = None
    store: bool = True
    # Not part of the Responses API. What the agent may do without asking: see steve_agent.interaction.
    mode: Mode = "auto"

    # Declared only so they can be rejected. See _reject_unsupported.
    tools: list[Any] = []
    tool_choice: Any = None
    text: dict[str, Any] = {}
    conversation: Any = None
    prompt: Any = None
    background: bool = False

    @model_validator(mode="before")
    @classmethod
    def _prepare(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Clients send explicit nulls for options they did not set; null means absent.
        data = {key: value for key, value in data.items() if value is not None}
        # Names only: values can hold user content or secrets.
        ignored = sorted(data.keys() - cls.model_fields.keys())
        if ignored:
            logger.debug("Ignoring request fields: %s", ", ".join(ignored))
        return data

    @field_validator("input", mode="before")
    @classmethod
    def _input_shape(cls, value: Any) -> Any:
        # Otherwise the union's error names a member: param "input.str", even for a list.
        if isinstance(value, str) or (isinstance(value, list) and all(isinstance(item, dict) for item in value)):
            return value
        raise ValueError("'input' must be a string or a list of objects.")

    # ApiError is not a ValueError, so pydantic lets it through to the app's handler
    # rather than turning it into a RequestValidationError.
    @model_validator(mode="after")
    def _reject_unsupported(self) -> Self:
        text_format = self.text.get("format")
        rejected = {
            "tools": bool(self.tools),
            "tool_choice": self.tool_choice not in (None, "auto", "none"),
            "text.format": isinstance(text_format, dict)
            and text_format.get("type") in ("json_schema", "json_object"),
            "conversation": self.conversation is not None,
            "prompt": self.prompt is not None,
            "background": self.background,
        }
        for param, is_rejected in rejected.items():
            if is_rejected:
                raise ApiError(
                    400,
                    f"This server does not support this value of '{param}'.",
                    param=param,
                    code="unsupported_parameter",
                )
        if not self.input:
            raise ApiError(400, "'input' must not be empty.", param="input", code="empty_input")
        return self
