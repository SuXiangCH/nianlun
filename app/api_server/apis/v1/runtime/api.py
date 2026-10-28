"""Public application execution, with all admission before SSE headers."""

import asyncio
import queue
import time
from typing import Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api_server.apis.v1.runtime_schemas import (
    PublicChatRequest,
    PublicImageAccessRequest,
)
from app.api_server.common.errors import ApiError, success
from app.api_server.common.request_context import get_request_id
from app.api_server.common.sse import encode_sse
from app.api_server.services.answer_image_streaming import (
    ImageStreamPrefix,
    utf8_chunks,
)

router = APIRouter(prefix="/api/v1/runtime/apps", tags=["public runtime"])
management_router = APIRouter(prefix="/api/v1/apps", tags=["application runs"])
images_router = APIRouter(prefix="/api/v1/runtime/images", tags=["public images"])


@router.post("/{application_id}/chat", response_model=None)
async def chat(
    application_id: str,
    body: PublicChatRequest,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    idempotency_key: str = Header(min_length=1, max_length=128),
) -> Any:
    service = request.app.state.services.invocations
    principal = await asyncio.to_thread(
        service.authenticate, application_id, authorization
    )
    preparation = asyncio.create_task(
        asyncio.to_thread(
            service.prepare,
            principal,
            body,
            idempotency_key,
            get_request_id(request),
            stream_output=body.response_mode == "streaming" and body.include_images,
        )
    )
    try:
        prepared = await asyncio.shield(preparation)
    except asyncio.CancelledError:
        prepared = await preparation
        service.cancel(prepared)
        raise
    response.headers["Location"] = service.location(prepared.row)
    response.headers["Cache-Control"] = "private, no-store"
    stop_monitoring = asyncio.Event()

    async def disconnected() -> None:
        while not stop_monitoring.is_set():
            if await request.is_disconnected():
                if stop_monitoring.is_set():
                    return
                service.cancel(prepared)
                raise ApiError("客户端已断开", 409)
            if stop_monitoring.is_set():
                return
            await asyncio.sleep(0.1)

    if body.response_mode == "blocking":
        result_task = asyncio.create_task(service.wait(prepared))
        disconnect_task = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait(
                [result_task, disconnect_task], return_when=asyncio.FIRST_COMPLETED
            )
            if result_task in done:
                return success(result_task.result().model_dump(mode="json"))
            disconnect_task.result()
        finally:
            # Starlette's receive probe may absorb task cancellation internally.
            stop_monitoring.set()
            result_task.cancel()
            disconnect_task.cancel()
            await asyncio.gather(result_task, disconnect_task, return_exceptions=True)
        return None

    async def event_stream():
        task = asyncio.create_task(service.wait(prepared))
        prefix = (
            ImageStreamPrefix(
                max_canonical_bytes=prepared.image_policy.max_canonical_bytes
            )
            if prepared.include_images
            else None
        )
        try:
            yield encode_sse(
                "ready",
                {
                    "run_id": prepared.row["id"],
                    "conversation_id": prepared.row["conversation_id"],
                    "release_id": prepared.row["release_id"],
                    "knowledge_base_content_version": prepared.row[
                        "knowledge_base_content_version"
                    ],
                },
            )
            heartbeat = time.monotonic()
            while not task.done() or (
                prepared.deltas is not None and not prepared.deltas.empty()
            ):
                if prepared.deltas is not None:
                    try:
                        delta = prepared.deltas.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        text = prefix.feed(delta) if prefix is not None else delta
                        for part in utf8_chunks(text):
                            yield encode_sse("message", {"delta": part})
                        continue
                if time.monotonic() - heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.01)
            response = await task
            if prepared.replay is None or body.include_images:
                suffix = (
                    prefix.remainder(response.answer)
                    if prefix is not None
                    else response.answer
                )
                for part in utf8_chunks(suffix):
                    yield encode_sse("message", {"delta": part})
            yield encode_sse("done", response.model_dump(mode="json"))
        except ApiError as exc:
            yield encode_sse(
                "error",
                {
                    "code": exc.status_code,
                    "message": exc.message,
                    "request_id": get_request_id(request),
                },
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if prepared.future is not None and not prepared.future.done():
                service.cancel(prepared)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Location": service.location(prepared.row),
        },
    )


@router.get("/{application_id}/runs/{run_id}")
def get_run(
    application_id: str,
    run_id: str,
    request: Request,
    response: Response,
    user_id: str = Query(min_length=1, max_length=128),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    service = request.app.state.services.invocations
    principal = service.authenticate(application_id, authorization)
    response.headers["Cache-Control"] = "private, no-store"
    return success(service.get(principal, run_id, user_id).model_dump(mode="json"))


@management_router.get("/{application_id}/runs")
def list_runs(
    application_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    return success(
        request.app.state.services.invocations.list(
            application_id,
            limit=limit,
            offset=offset,
            status=status,
            client_id=client_id,
        )
    )


@images_router.get("/{grant_id}")
def read_public_image(
    grant_id: str, request: Request, token: str = Query(min_length=1, max_length=128)
) -> Response:
    data, media_type = request.app.state.services.invocations.image_access.read(
        grant_id, token
    )
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/{application_id}/runs/{run_id}/images/{attachment_id}/access")
def refresh_public_image(
    application_id: str,
    run_id: str,
    attachment_id: str,
    body: PublicImageAccessRequest,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    service = request.app.state.services.invocations
    principal = service.authenticate(application_id, authorization)
    response.headers["Cache-Control"] = "private, no-store"
    return success(
        service.image_access.refresh(
            principal, run_id, attachment_id, body.user_id
        ).model_dump(mode="json")
    )


__all__ = ["router", "management_router", "images_router"]
