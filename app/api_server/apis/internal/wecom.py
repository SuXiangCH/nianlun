"""Internal gateway contract: snapshot, status reports, and NDJSON runs."""

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api_server.apis.v1.publishing_schemas import WecomStatusRequest
from app.api_server.common.errors import ApiError
from app.api_server.common.request_context import get_request_id

router = APIRouter(
    prefix="/internal/v1/wecom",
    tags=["internal wecom"],
    include_in_schema=False,
)

_NO_STORE = {"Cache-Control": "no-store"}


class WecomRunMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_key: str = Field(pattern=r"^wc_[0-9a-f]{32}$")
    text: str = Field(min_length=1)


class WecomRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    message: WecomRunMessage


def _authorize(request: Request, authorization: str | None) -> None:
    expected = request.app.state.settings.internal_token
    if not expected:
        raise ApiError("内部接口未启用", 503, headers=_NO_STORE)
    scheme, _, provided = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not provided or provided.strip() != provided:
        raise ApiError("内部凭据无效", 401, headers=_NO_STORE)
    if not hmac.compare_digest(
        hashlib.sha256(expected.encode()).hexdigest(),
        hashlib.sha256(provided.encode()).hexdigest(),
    ):
        raise ApiError("内部凭据无效", 401, headers=_NO_STORE)


def _wecom(request: Request) -> Any:
    service = getattr(request.app.state.services, "wecom", None)
    if service is None:
        raise ApiError("企业微信渠道未启用", 503, headers=_NO_STORE)
    return service


@router.get("/snapshot")
def snapshot(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _authorize(request, authorization)
    response = _wecom(request).snapshot()
    return JSONResponse(content=response, headers=_NO_STORE)


@router.post("/channels/{channel_id}/status", status_code=204)
def report_status(
    channel_id: str,
    body: WecomStatusRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    _authorize(request, authorization)
    _wecom(request).report_status(channel_id, body)
    return Response(status_code=204, headers=_NO_STORE)


@router.post("/channels/{channel_id}/runs", response_model=None)
async def create_run(
    channel_id: str,
    body: WecomRunRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    idempotency_key: str = Header(min_length=1, max_length=128),
) -> Any:
    _authorize(request, authorization)
    service = request.app.state.services.invocations
    started = time.monotonic()
    prepared = await asyncio.to_thread(
        service.prepare_channel,
        channel_id,
        body.revision,
        body.message.user_key,
        body.message.text,
        idempotency_key,
        get_request_id(request),
        started,
    )
    if prepared.duplicate:
        return Response(status_code=204, headers=_NO_STORE)

    async def events():
        async for event in service.stream_channel(prepared):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers=_NO_STORE,
    )


__all__ = ["router"]
