"""Request-scoped context shared by the API and service layers."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, MutableMapping

from fastapi import Request

REQUEST_ID_SCOPE_KEY = "nianlun_request_id"

_request_id: ContextVar[str | None] = ContextVar("nianlun_request_id", default=None)


def set_request_id(value: str) -> Any:
    """Set the request id for the current async context."""
    return _request_id.set(value)


def reset_request_id(token: Any) -> None:
    """Restore the request id that existed before the current request."""
    _request_id.reset(token)


def current_request_id() -> str | None:
    """Return the request id of the current request, if one exists."""
    return _request_id.get()


def get_request_id_from_scope(scope: MutableMapping[str, Any]) -> str:
    """Read the request id stored by :class:`RequestTrackingMiddleware`."""
    value = scope.get(REQUEST_ID_SCOPE_KEY)
    return value if isinstance(value, str) else "unknown"


def get_request_id(request: Request) -> str:
    """Read a request id from a FastAPI request."""
    return get_request_id_from_scope(request.scope)


__all__ = [
    "REQUEST_ID_SCOPE_KEY",
    "current_request_id",
    "get_request_id",
    "get_request_id_from_scope",
    "reset_request_id",
    "set_request_id",
]
