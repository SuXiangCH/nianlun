"""OpenAI-compatible text completions over the published Agent runtime."""

import asyncio
from datetime import datetime
import json
import queue
import time
from typing import Any
import uuid

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import StreamingResponse

from app.api_server.apis.v1.openai_schemas import (
    AssistantMessage,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChunkChoice,
    CompletionChoice,
    CompletionDelta,
    CompletionUsage,
    ModelListResponse,
    ModelResponse,
)
from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
from app.api_server.common.errors import ApiError, openai_error
from app.api_server.common.request_context import get_request_id
from app.api_server.services.application_invocation_service import (
    ApplicationInvocationService,
    PreparedInvocation,
)
from app.api_server.services.answer_image_streaming import (
    ImageStreamPrefix,
    utf8_chunks,
)

router = APIRouter(prefix="/v1", tags=["OpenAI compatible"])


def _service(request: Request) -> ApplicationInvocationService:
    return request.app.state.services.invocations


def _model(
    service: ApplicationInvocationService,
    authorization: str | None,
    model_id: str | None = None,
) -> ModelResponse:
    principal = service.authenticate(model_id, authorization)
    release = service.publishing.runtime_release(principal)
    return ModelResponse(
        id=principal.application_id,
        created=int(release.created_at.timestamp()),
    )


@router.get("/models")
def list_models(
    request: Request, authorization: str | None = Header(default=None)
) -> ModelListResponse:
    return ModelListResponse(data=[_model(_service(request), authorization)])


@router.get("/models/{model_id}")
def get_model(
    model_id: str, request: Request, authorization: str | None = Header(default=None)
) -> ModelResponse:
    return _model(_service(request), authorization, model_id)


def _usage(usage: dict[str, int] | None) -> CompletionUsage | None:
    if usage is None:
        return None
    return CompletionUsage(
        prompt_tokens=usage.get("input_tokens", 0),
        completion_tokens=usage.get("output_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
    )


def _identity(prepared: PreparedInvocation) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{prepared.row['id']}",
        "created": int(datetime.fromisoformat(prepared.row["created_at"]).timestamp()),
        "model": prepared.row["application_id"],
    }


def _data(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _blocking(
    service: ApplicationInvocationService,
    prepared: PreparedInvocation,
    request: Request,
) -> ChatCompletionResponse:
    stopped = asyncio.Event()

    async def monitor() -> None:
        while not stopped.is_set():
            if await request.is_disconnected() and not stopped.is_set():
                service.cancel(prepared)
                raise ApiError("客户端已断开", 409)
            await asyncio.sleep(0.1)

    task = asyncio.create_task(service.wait(prepared))
    disconnected = asyncio.create_task(monitor())
    try:
        done, _ = await asyncio.wait(
            [task, disconnected], return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            result = task.result()
            return ChatCompletionResponse(
                **_identity(prepared),
                choices=[
                    CompletionChoice(message=AssistantMessage(content=result.answer))
                ],
                usage=_usage(result.usage),
            )
        disconnected.result()
        raise ApiError("客户端已断开", 409)
    finally:
        stopped.set()
        task.cancel()
        disconnected.cancel()
        await asyncio.gather(task, disconnected, return_exceptions=True)
        if prepared.future is not None and not prepared.future.done():
            service.cancel(prepared)


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def create_completion(
    body: ChatCompletionRequest,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, min_length=1, max_length=128),
    x_nianlun_include_images: str = Header(default="false"),
    x_nianlun_image_transport: str = Header(default="url"),
) -> Any:
    if x_nianlun_include_images not in {
        "true",
        "false",
    } or x_nianlun_image_transport not in {"url", "data_url"}:
        raise ApiError("图片请求头格式无效", 400)
    if x_nianlun_include_images == "false" and x_nianlun_image_transport == "data_url":
        raise ApiError("内联图片需要 X-Nianlun-Include-Images: true", 400)
    service = _service(request)
    principal = await asyncio.to_thread(service.authenticate, body.model, authorization)
    messages = body.runtime_messages()
    native = PublicChatRequest(
        message=messages[-1]["content"],
        user_id=body.user or "openai",
        response_mode="streaming" if body.stream else "blocking",
        include_images=x_nianlun_include_images == "true",
        image_transport="data_url"
        if x_nianlun_image_transport == "data_url"
        else "url",
    )
    preparation = asyncio.create_task(
        asyncio.to_thread(
            service.prepare,
            principal,
            native,
            idempotency_key or str(uuid.uuid4()),
            get_request_id(request),
            input_messages=messages,
            stream_output=body.stream,
        )
    )
    try:
        prepared = await asyncio.shield(preparation)
    except asyncio.CancelledError:
        prepared = await preparation
        service.cancel(prepared)
        raise
    if not body.stream:
        response.headers["Location"] = service.location(prepared.row)
        response.headers["Cache-Control"] = "private, no-store"
        return await _blocking(service, prepared, request)

    async def events():
        task = asyncio.create_task(service.wait(prepared))
        identity = _identity(prepared)
        prefix = (
            ImageStreamPrefix(
                max_canonical_bytes=prepared.image_policy.max_canonical_bytes
            )
            if prepared.include_images
            else None
        )

        def chunk(delta: CompletionDelta, *, finished: bool = False) -> str:
            payload = ChatCompletionChunk(
                **identity,
                choices=[
                    ChunkChoice(delta=delta, finish_reason="stop" if finished else None)
                ],
            ).model_dump(mode="json", exclude_none=True)
            payload["choices"][0]["finish_reason"] = "stop" if finished else None
            return _data(payload)

        try:
            yield chunk(CompletionDelta(role="assistant", content=""))
            heartbeat = time.monotonic()
            while not task.done() or (
                prepared.deltas is not None and not prepared.deltas.empty()
            ):
                if prepared.deltas is not None:
                    try:
                        text = prepared.deltas.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        if prefix is not None:
                            text = prefix.feed(text)
                        for part in utf8_chunks(text):
                            yield chunk(CompletionDelta(content=part))
                        continue
                if time.monotonic() - heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.01)
            result = await task
            if prepared.replay is not None or prepared.include_images:
                suffix = (
                    prefix.remainder(result.answer)
                    if prefix is not None
                    else result.answer
                )
                for part in utf8_chunks(suffix):
                    yield chunk(CompletionDelta(content=part))
            yield chunk(CompletionDelta(), finished=True)
            if body.stream_options is not None and body.stream_options.include_usage:
                yield _data(
                    ChatCompletionChunk(
                        **identity, choices=[], usage=_usage(result.usage)
                    ).model_dump(mode="json")
                )
            yield "data: [DONE]\n\n"
        except ApiError as exc:
            yield _data(openai_error(exc.message, exc.status_code))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if prepared.future is not None and not prepared.future.done():
                service.cancel(prepared)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Accel-Buffering": "no",
            "Location": service.location(prepared.row),
        },
    )
