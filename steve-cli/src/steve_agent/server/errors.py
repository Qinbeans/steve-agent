import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """A request failure that maps onto the OpenAI error envelope."""

    def __init__(self, status: int, message: str, *, param: str | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.param = param
        self.code = code


def error_response(
    status: int,
    message: str,
    *,
    param: str | None = None,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error_type = "server_error" if status >= 500 else "invalid_request_error"
    return JSONResponse(
        {"error": {"message": message, "type": error_type, "param": param, "code": code}},
        status_code=status,
        # The SDK otherwise retries 5xx and 429 twice, and a retried run writes files again.
        headers={**(headers or {}), "x-should-retry": "false"},
    )


def add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return error_response(exc.status, exc.message, param=exc.param, code=exc.code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0]
        # For unparseable JSON the location holds a character offset, not a field.
        param = None if first["type"] == "json_invalid" else ".".join(str(part) for part in first["loc"][1:]) or None
        return error_response(400, first["msg"], param=param, code="invalid_request")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(exc.status_code, str(exc.detail), headers=dict(exc.headers or {}))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error("Unhandled error on %s", request.url.path, exc_info=exc)
        return error_response(500, "The server failed to process the request.", code="server_error")
