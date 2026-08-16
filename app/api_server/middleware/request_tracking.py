"""Request correlation, response headers, and access logging."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api_server.common.request_context import (
    REQUEST_ID_SCOPE_KEY,
    reset_request_id,
    set_request_id,
)

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _client_ip(scope: Scope) -> str | None:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        value = client[0]
        return value if isinstance(value, str) else str(value)
    return None


def _request_id(headers: Headers) -> str:
    supplied = headers.get(REQUEST_ID_HEADER)
    if supplied and _VALID_REQUEST_ID.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


class RequestTrackingMiddleware:
    """Attach a correlation id and log the lifecycle of every HTTP request.

    The middleware is implemented as pure ASGI so it also covers streaming
    responses without buffering their body. It intentionally logs metadata
    only; request and response bodies can contain sensitive user content.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = _request_id(headers)
        scope[REQUEST_ID_SCOPE_KEY] = request_id
        context_token = set_request_id(request_id)
        start = time.perf_counter()
        status_code: int | None = None
        completed = False
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        client_ip = _client_ip(scope)

        logger.info(
            "request.started request_id=%s method=%s path=%s client_ip=%s",
            request_id,
            method,
            path,
            client_ip or "-",
            extra={
                "request_id": request_id,
                "method": method,
                "path": path,
                "client_ip": client_ip,
            },
        )

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, completed
            if message["type"] == "http.response.start":
                status_code = message.get("status")
                response_headers = MutableHeaders(raw=message["headers"])
                response_headers[REQUEST_ID_HEADER] = request_id
            is_final_body = message["type"] == "http.response.body" and not message.get(
                "more_body", False
            )
            await send(message)
            if is_final_body:
                completed = True
                self._log_completed(
                    request_id,
                    method,
                    path,
                    status_code or 500,
                    start,
                )

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            if not completed:
                logger.exception(
                    "request.failed request_id=%s method=%s path=%s duration_ms=%.2f",
                    request_id,
                    method,
                    path,
                    self._duration_ms(start),
                    extra={
                        "request_id": request_id,
                        "method": method,
                        "path": path,
                        "status_code": status_code or 500,
                        "duration_ms": self._duration_ms(start),
                    },
                )
            raise
        finally:
            reset_request_id(context_token)

    @staticmethod
    def _duration_ms(start: float) -> float:
        return round((time.perf_counter() - start) * 1000, 2)

    @classmethod
    def _log_completed(
        cls,
        request_id: str,
        method: str,
        path: str,
        status_code: int,
        start: float,
    ) -> None:
        duration_ms = cls._duration_ms(start)
        level = (
            logging.ERROR
            if status_code >= 500
            else logging.WARNING
            if status_code >= 400
            else logging.INFO
        )
        logger.log(
            level,
            "request.completed request_id=%s method=%s path=%s status_code=%s duration_ms=%.2f",
            request_id,
            method,
            path,
            status_code,
            duration_ms,
            extra={
                "request_id": request_id,
                "method": method,
                "path": path,
                "status_code": status_code,
                "duration_ms": duration_ms,
            },
        )


__all__ = ["REQUEST_ID_HEADER", "RequestTrackingMiddleware"]
