"""FastAPI control plane for the standalone channel gateway."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Any, Protocol

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.channel_gateway.config import GatewaySettings
from app.channel_gateway.protocol import GatewayError


class GatewayControl(Protocol):
    """Control surface implemented by the channel manager."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def readiness(self) -> bool: ...

    async def ensure_connected(
        self, channel_id: str, revision: int, request_id: str
    ) -> dict[str, Any]: ...

    def request_refresh(self) -> bool: ...

    def metrics_snapshot(self) -> dict[str, int]: ...


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(token: str) -> bytes:
    return sha256(token.encode("utf-8")).digest()


def _manager(request: Request) -> GatewayControl:
    manager: GatewayControl = request.app.state.manager
    return manager


async def _gateway_error(_: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, GatewayError):
        raise error
    return JSONResponse(
        status_code=error.status_code,
        content={"code": error.code, "message": error.message},
    )


async def _validation_error(_: Request, error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"code": "INVALID_REQUEST", "message": "请求参数无效"},
    )


def authorize(request: Request) -> None:
    settings: GatewaySettings = request.app.state.settings
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if (
        scheme.lower() != "bearer"
        or not value
        or not hmac.compare_digest(_digest(value), _digest(settings.control_token))
    ):
        raise GatewayError("UNAUTHORIZED", status_code=401)


async def health() -> dict[str, str]:
    return {"status": "ok"}


async def ready(request: Request) -> dict[str, str]:
    if not await _manager(request).readiness():
        raise GatewayError("NOT_READY", status_code=503)
    return {"status": "ready"}


async def connect_channel(payload: ConnectRequest, request: Request) -> dict[str, Any]:
    return await _manager(request).ensure_connected(
        payload.channel_id, payload.revision, payload.request_id
    )


async def refresh_channels(
    request: Request, payload: RefreshRequest | None = None
) -> dict[str, str]:
    if not _manager(request).request_refresh():
        raise GatewayError("GATEWAY_STOPPED", status_code=503)
    return {"status": "accepted"}


async def gateway_metrics(request: Request) -> PlainTextResponse:
    lines = [
        f"channel_gateway_{name} {value}"
        for name, value in _manager(request).metrics_snapshot().items()
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


def create_app(
    manager: GatewayControl,
    settings: GatewaySettings,
    *,
    start_manager: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_manager:
            await manager.start()
        try:
            yield
        finally:
            if start_manager:
                await manager.stop()

    app = FastAPI(title="Nianlun WeCom Channel Gateway", lifespan=lifespan)
    app.state.manager = manager
    app.state.settings = settings
    app.add_exception_handler(GatewayError, _gateway_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/ready", ready, methods=["GET"])
    app.add_api_route(
        "/api/channels/connect",
        connect_channel,
        methods=["POST"],
        dependencies=[Depends(authorize)],
    )
    app.add_api_route(
        "/api/channels/refresh",
        refresh_channels,
        methods=["POST"],
        status_code=202,
        dependencies=[Depends(authorize)],
    )
    app.add_api_route(
        "/internal/metrics",
        gateway_metrics,
        methods=["GET"],
        dependencies=[Depends(authorize)],
    )
    return app


__all__ = [
    "ConnectRequest",
    "GatewayControl",
    "RefreshRequest",
    "create_app",
]
