"""Blocking and Server-Sent Events chat routes."""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api_server.apis.v1.schemas import ChatRequest
from app.api_server.common.errors import ApiError, success
from app.api_server.common.request_context import get_request_id
from app.api_server.common.sse import encode_sse
from app.api_server.services.container import ApiServices

router = APIRouter(prefix="/api/v1/apps", tags=["chat"])
logger = logging.getLogger(__name__)


def _services(request: Request) -> ApiServices:
    return request.app.state.services


@router.post("/{application_id}/chat", response_model=None)
async def chat(
    application_id: str, body: ChatRequest, request: Request
) -> dict[str, Any] | StreamingResponse:
    service = _services(request).chat
    if body.response_mode == "blocking":
        response = await service.complete_async(
            application_id,
            body.message,
            body.conversation_id,
            clarification_enabled=body.clarification_enabled,
        )
        return success(response.model_dump(mode="json"))

    conversation_id, message_id, events = service.stream(
        application_id,
        body.message,
        body.conversation_id,
        clarification_enabled=body.clarification_enabled,
    )

    async def event_stream():
        try:
            yield encode_sse("ready", {"conversation_id": conversation_id})
            async for event in service.iterate_stream_async(events):
                event_type = event["type"]
                payload = event["data"]
                if event_type == "done":
                    payload = {
                        "app_id": application_id,
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                        "answer": payload.get("answer", ""),
                        "route": payload.get("route", "direct"),
                        "retrieved_snippets": payload.get("retrieved_snippets", []),
                        "status_events": payload.get("status_events", []),
                        "tool_calls": payload.get("tool_calls", []),
                        "usage": payload.get("usage"),
                        "ttft_ms": payload.get("ttft_ms"),
                        "clarification": payload.get("clarification"),
                    }
                yield encode_sse(event_type, payload)
        except ApiError as exc:
            logger.warning(
                "chat.stream_error request_id=%s status_code=%s",
                get_request_id(request),
                exc.status_code,
            )
            yield encode_sse(
                "error",
                {
                    "code": exc.status_code,
                    "message": exc.message,
                    "request_id": get_request_id(request),
                },
            )
        except Exception:
            logger.exception("chat.stream_error request_id=%s", get_request_id(request))
            yield encode_sse(
                "error",
                {
                    "code": 500,
                    "message": "服务内部错误",
                    "request_id": get_request_id(request),
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/{application_id}/conversations")
def list_conversations(application_id: str, request: Request) -> dict[str, Any]:
    items = _services(request).chat.list_conversations(application_id)
    return success([item.model_dump(mode="json") for item in items])


@router.get("/{application_id}/conversations/{conversation_id}/messages")
def list_messages(
    application_id: str, conversation_id: str, request: Request
) -> dict[str, Any]:
    items = _services(request).chat.messages(application_id, conversation_id)
    return success([item.model_dump(mode="json") for item in items])


@router.get(
    "/{application_id}/conversations/{conversation_id}/messages/"
    "{message_id}/sources/{source_id}"
)
def get_source(
    application_id: str,
    conversation_id: str,
    message_id: str,
    source_id: str,
    request: Request,
) -> dict[str, Any]:
    item = _services(request).chat.source(
        application_id,
        conversation_id,
        message_id,
        source_id,
    )
    return success(item.model_dump(mode="json"))


@router.delete("/{application_id}/conversations/{conversation_id}")
def delete_conversation(
    application_id: str, conversation_id: str, request: Request
) -> dict[str, Any]:
    _services(request).chat.delete_conversation(application_id, conversation_id)
    return success(None)


__all__ = ["router"]
