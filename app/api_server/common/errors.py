"""HTTP-facing domain exceptions and exception handlers."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api_server.common.request_context import get_request_id

logger = logging.getLogger(__name__)


def is_openai_request(request: Request) -> bool:
    return request.url.path.startswith("/v1/")


def openai_error(
    message: str, status_code: int, *, param: str | None = None
) -> dict[str, Any]:
    error_type = {
        401: "authentication_error",
        403: "permission_error",
        404: "invalid_request_error",
        429: "rate_limit_error",
    }.get(
        status_code, "server_error" if status_code >= 500 else "invalid_request_error"
    )
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": str(status_code),
        }
    }


class ApiError(Exception):
    """An expected domain error that can be safely returned to an API client."""

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.headers = headers or {}


def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):
        return internal_error_handler(_request, exc)
    return JSONResponse(
        status_code=exc.status_code,
        content=openai_error(exc.message, exc.status_code)
        if is_openai_request(_request)
        else {"code": exc.status_code, "message": exc.message, "data": None},
        headers={**exc.headers, "X-Request-Id": get_request_id(_request)},
    )


def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        return internal_error_handler(_request, exc)
    if is_openai_request(_request):
        # Only schema paths are public; validation input and exception context may contain secrets.
        location = exc.errors()[0].get("loc", ())
        param = (
            ".".join(
                str(item)
                for item in location
                if item not in {"body", "header", "query"}
            )
            or None
        )
        return JSONResponse(
            status_code=400,
            content=openai_error("请求参数不受支持或格式无效", 400, param=param),
            headers={"X-Request-Id": get_request_id(_request)},
        )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "code": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "message": "请求参数校验失败",
            "data": {
                "errors": jsonable_encoder(
                    [
                        {
                            key: value
                            for key, value in error.items()
                            if key not in {"input", "ctx"}
                        }
                        for error in exc.errors()
                    ]
                )
            },
        },
        headers={"X-Request-Id": get_request_id(_request)},
    )


def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        return internal_error_handler(_request, exc)
    message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return JSONResponse(
        status_code=exc.status_code,
        content=openai_error(message, exc.status_code)
        if is_openai_request(_request)
        else {"code": exc.status_code, "message": message, "data": None},
        headers={"X-Request-Id": get_request_id(_request)},
    )


def internal_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
    logger.error(
        "request.internal_error request_id=%s path=%s",
        get_request_id(_request),
        _request.url.path,
        exc_info=(_exc.__class__, _exc, _exc.__traceback__),
        extra={
            "request_id": get_request_id(_request),
            "path": _request.url.path,
            "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
        },
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=openai_error("服务内部错误", 500)
        if is_openai_request(_request)
        else {
            "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
            "message": "服务内部错误",
            "data": None,
        },
        headers={"X-Request-Id": get_request_id(_request)},
    )


def success(data: Any) -> dict[str, Any]:
    return {"code": status.HTTP_200_OK, "message": "success", "data": data}


__all__ = [
    "ApiError",
    "api_error_handler",
    "internal_error_handler",
    "http_exception_handler",
    "success",
    "validation_error_handler",
]
