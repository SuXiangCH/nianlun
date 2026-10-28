"""Conversation service shared by blocking and streaming HTTP routes."""

from __future__ import annotations

from contextlib import nullcontext

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import status

from nianlun.agent.lead_agent.runtime import (
    AgentRuntime,
    iter_agent_stream_events,
    run_agent,
)
from nianlun.agent.prompt_registry import (
    UnsupportedPromptBundle,
    resolve_prompt_contract_json,
)
from app.api_server.apis.v1.schemas import (
    ChatResponse,
    ConversationMessageResponse,
    ConversationResponse,
    MessageSourceResponse,
)
from app.api_server.common.errors import ApiError
from app.api_server.config import ApiServerSettings
from nianlun.agent.contracts import AnswerImagePolicy
from app.api_server.repositories.sqlite_chat import SQLiteChatRepository
from app.api_server.services.application_service import ApplicationService
from app.api_server.services.answer_image_service import AnswerImageService

logger = logging.getLogger(__name__)
_STREAM_END = object()


class _CancellableEventStream:
    def __init__(
        self,
        events: Iterator[dict[str, Any]],
        cancellation_event: threading.Event,
    ) -> None:
        self._events = events
        self._cancellation_event = cancellation_event

    def __iter__(self) -> _CancellableEventStream:
        return self

    def __next__(self) -> dict[str, Any]:
        return next(self._events)

    def cancel(self) -> None:
        self._cancellation_event.set()

    def close(self) -> None:
        self.cancel()
        close = getattr(self._events, "close", None)
        if callable(close):
            close()


def _log_runtime(application_id: str, runtime: Any) -> None:
    """Log the effective chat endpoint without exposing credentials."""
    logger.info(
        "chat.runtime_selected application_id=%s model=%s base_url=%s",
        application_id,
        getattr(runtime, "model", "unknown"),
        getattr(runtime, "effective_url", "unknown"),
    )


class ChatService:
    def __init__(
        self,
        applications: ApplicationService,
        repository: SQLiteChatRepository,
        image_service: AnswerImageService | None = None,
        settings: ApiServerSettings | None = None,
    ) -> None:
        self.applications = applications
        self.repository = repository
        self.image_service = image_service
        self.settings = settings or ApiServerSettings()
        self._conversation_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._stream_executor = ThreadPoolExecutor(
            max_workers=32, thread_name_prefix="nianlun-chat-stream"
        )

    def _conversation_lock(self, key: str) -> threading.RLock:
        with self._locks_guard:
            return self._conversation_locks.setdefault(key, threading.RLock())

    @staticmethod
    def _conversation_id(conversation_id: str | None) -> str:
        return conversation_id or str(uuid.uuid4())

    def _runtime(
        self, application_id: str, contract: str | None, policy: AnswerImagePolicy
    ) -> AgentRuntime:
        try:
            if contract is None:
                raise UnsupportedPromptBundle("Missing console contract")
            bundle = resolve_prompt_contract_json(contract)
        except UnsupportedPromptBundle as exc:
            raise ApiError("会话版本不兼容，请新建会话", 409) from exc
        return self.applications.runtime(
            application_id,
            prompt_bundle=bundle,
            image_capability=(
                bundle.supports_images
                and self.image_service is not None
                and self.settings.answer_images_enabled
            ),
            image_policy=policy,
        )

    def complete(
        self,
        application_id: str,
        message: str,
        conversation_id: str | None,
        *,
        clarification_enabled: bool = False,
    ) -> ChatResponse:
        active_conversation_id = self._conversation_id(conversation_id)
        policy = self.settings.freeze_image_policy()
        assistant_message_id = str(uuid.uuid4())
        lock = self._conversation_lock(f"{application_id}:{active_conversation_id}")
        with lock:
            self.applications.require_record(application_id)
            try:
                contract = self.repository.begin_turn(
                    application_id=application_id,
                    conversation_id=active_conversation_id,
                    user_message_id=str(uuid.uuid4()),
                    assistant_message_id=assistant_message_id,
                    user_content=message,
                    now=_now(),
                )
            except ValueError as exc:
                raise ApiError(str(exc), status.HTTP_409_CONFLICT) from exc
            try:
                runtime = self._runtime(application_id, contract, policy)
                _log_runtime(application_id, runtime)
                result = run_agent(
                    runtime,
                    message,
                    thread_id=f"{application_id}:{active_conversation_id}",
                    clarification_enabled=clarification_enabled,
                    trusted_history=self.repository.trusted_history(
                        application_id, active_conversation_id
                    ),
                )
                with (
                    self.image_service.completion_scope()
                    if self.image_service
                    else nullcontext()
                ):
                    result = self._prepare_result(
                        application_id, result, policy=policy, question=message
                    )
                    self.repository.complete_turn(
                        application_id=application_id,
                        conversation_id=active_conversation_id,
                        assistant_message_id=assistant_message_id,
                        answer=result["answer"],
                        route=result.get("route", "direct"),
                        snippets=result.get("retrieved_snippets", []),
                        tool_calls=result.get("tool_calls", []),
                        trace=result.get("trace", []),
                        usage=result.get("usage"),
                        ttft_ms=result.get("ttft_ms"),
                        images=result.get("images", []),
                        image_policy=policy,
                        answer_format_version=result.get("answer_format_version", 1),
                        answer_output_mode=str(
                            result.get("answer_output_mode", "text")
                        ),
                        image_previews_unavailable=result.get(
                            "image_previews_unavailable", 0
                        ),
                        now=_now(),
                    )
            except Exception as exc:
                self._mark_failed(
                    application_id,
                    active_conversation_id,
                    assistant_message_id,
                    exc,
                )
                raise
        result = self._console_result(
            application_id, active_conversation_id, assistant_message_id, result
        )
        return ChatResponse(
            app_id=application_id,
            conversation_id=active_conversation_id,
            message_id=assistant_message_id,
            answer=result["answer"],
            route=result.get("route", "direct"),
            retrieved_snippets=result.get("retrieved_snippets", []),
            status_events=result.get("status_events", []),
            tool_calls=result.get("tool_calls", []),
            trace=result.get("trace", []),
            usage=result.get("usage"),
            ttft_ms=result.get("ttft_ms"),
            clarification=result.get("clarification"),
            images=result.get("images", []),
            answer_format_version=result.get("answer_format_version", 1),
            answer_output_mode=result.get("answer_output_mode", "text"),
            image_previews_unavailable=result.get("image_previews_unavailable", 0),
        )

    async def complete_async(
        self,
        application_id: str,
        message: str,
        conversation_id: str | None,
        *,
        clarification_enabled: bool = False,
    ) -> ChatResponse:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._stream_executor,
            lambda: self.complete(
                application_id,
                message,
                conversation_id,
                clarification_enabled=clarification_enabled,
            ),
        )

    def stream(
        self,
        application_id: str,
        message: str,
        conversation_id: str | None,
        *,
        clarification_enabled: bool = False,
    ) -> tuple[str, str, Iterator[dict[str, Any]]]:
        active_conversation_id = self._conversation_id(conversation_id)
        policy = self.settings.freeze_image_policy()
        message_id = str(uuid.uuid4())
        cancellation_event = threading.Event()

        lock = self._conversation_lock(f"{application_id}:{active_conversation_id}")

        def locked_events() -> Iterator[dict[str, Any]]:
            with lock:
                turn_started = False
                turn_completed = False
                turn_failed = False
                runtime: Any | None = None
                try:
                    self.applications.require_record(application_id)
                    try:
                        contract = self.repository.begin_turn(
                            application_id=application_id,
                            conversation_id=active_conversation_id,
                            user_message_id=str(uuid.uuid4()),
                            assistant_message_id=message_id,
                            user_content=message,
                            now=_now(),
                        )
                    except ValueError as exc:
                        raise ApiError(str(exc), status.HTTP_409_CONFLICT) from exc
                    turn_started = True
                    runtime = self._runtime(application_id, contract, policy)
                    _log_runtime(application_id, runtime)
                    for event in iter_agent_stream_events(
                        runtime,
                        message,
                        thread_id=f"{application_id}:{active_conversation_id}",
                        clarification_enabled=clarification_enabled,
                        cancellation_event=cancellation_event,
                        trusted_history=self.repository.trusted_history(
                            application_id, active_conversation_id
                        ),
                    ):
                        if event.get("type") == "done":
                            with (
                                self.image_service.completion_scope()
                                if self.image_service
                                else nullcontext()
                            ):
                                payload = self._prepare_result(
                                    application_id,
                                    event["data"],
                                    policy=policy,
                                    question=message,
                                )
                                self.repository.complete_turn(
                                    application_id=application_id,
                                    conversation_id=active_conversation_id,
                                    assistant_message_id=message_id,
                                    answer=payload.get("answer", ""),
                                    route=payload.get("route", "direct"),
                                    snippets=payload.get("retrieved_snippets", []),
                                    tool_calls=payload.get("tool_calls", []),
                                    trace=payload.get("trace", []),
                                    usage=payload.get("usage"),
                                    ttft_ms=payload.get("ttft_ms"),
                                    images=payload.get("images", []),
                                    image_policy=policy,
                                    answer_format_version=payload.get(
                                        "answer_format_version", 1
                                    ),
                                    answer_output_mode=str(
                                        payload.get("answer_output_mode", "text")
                                    ),
                                    image_previews_unavailable=payload.get(
                                        "image_previews_unavailable", 0
                                    ),
                                    now=_now(),
                                )
                            event = {
                                "type": "done",
                                "data": self._console_result(
                                    application_id,
                                    active_conversation_id,
                                    message_id,
                                    payload,
                                ),
                            }
                            turn_completed = True
                        yield event
                except Exception as exc:
                    if runtime is not None:
                        logger.exception(
                            "chat.agent_failed application_id=%s model=%s base_url=%s",
                            application_id,
                            getattr(runtime, "model", "unknown"),
                            getattr(runtime, "effective_url", "unknown"),
                        )
                    if not turn_started:
                        raise
                    turn_failed = True
                    self._mark_failed(
                        application_id,
                        active_conversation_id,
                        message_id,
                        exc,
                    )
                    raise
                finally:
                    # GeneratorExit is raised when the HTTP client disconnects
                    # and is not caught by ``except Exception``. Close out the
                    # durable turn so history cannot retain a forever-pending
                    # assistant message.
                    if turn_started and not turn_completed and not turn_failed:
                        self._mark_failed(
                            application_id,
                            active_conversation_id,
                            message_id,
                            RuntimeError("流式请求未正常结束"),
                        )

        events = _CancellableEventStream(locked_events(), cancellation_event)
        return active_conversation_id, message_id, events

    def _prepare_result(
        self,
        application_id: str,
        result: dict[str, Any],
        *,
        policy: AnswerImagePolicy = AnswerImagePolicy(),
        question: str = "",
    ) -> dict[str, Any]:
        if self.image_service is None or not self.settings.answer_images_enabled:
            return {**result, "images": [], "answer_output_mode": "text"}
        return self.image_service.prepare_result(
            application_id, result, policy=policy, question=question
        )

    @staticmethod
    def _source_projection(
        source: dict[str, Any], images: list[dict[str, Any]]
    ) -> dict[str, Any]:
        value = {key: item for key, item in source.items() if key != "images"}
        value["image_attachment_ids"] = [
            image["id"]
            for image in images
            if source.get("citation_id") in image.get("citation_ids", [])
        ]
        return value

    def _console_result(
        self,
        application_id: str,
        conversation_id: str,
        message_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        images = (
            self.image_service.console_images(
                application_id, conversation_id, message_id
            )
            if self.image_service is not None
            else []
        )
        persisted = self.repository.get_messages(application_id, conversation_id) or []
        message = next((item for item in persisted if item["id"] == message_id), None)
        return {
            **result,
            **(
                {
                    "answer": message["content"],
                    "answer_output_mode": message["answer_output_mode"],
                }
                if message
                else {}
            ),
            "images": images,
            "retrieved_snippets": [
                self._source_projection(source, images)
                for source in result.get("retrieved_snippets", [])
            ],
        }

    async def iterate_stream_async(
        self, events: Iterator[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        """Bridge one blocking stream from a dedicated worker to the event loop."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=1)
        stop = threading.Event()

        def put(item: object) -> bool:
            pending = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            while not stop.is_set():
                try:
                    pending.result(timeout=0.1)
                    return True
                except FutureTimeoutError:
                    continue
            pending.cancel()
            return False

        def produce() -> None:
            try:
                for event in events:
                    if stop.is_set() or not put(event):
                        return
            except BaseException as exc:
                if not stop.is_set():
                    put(exc)
            finally:
                close = getattr(events, "close", None)
                if callable(close):
                    close()
                if not stop.is_set():
                    put(_STREAM_END)

        worker = self._stream_executor.submit(produce)
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item  # type: ignore[misc]
        finally:
            cancel = getattr(events, "cancel", None)
            if callable(cancel):
                cancel()
            stop.set()
            if worker.done():
                worker.result()

    def list_conversations(
        self, application_id: str, channel: str = "console"
    ) -> list[ConversationResponse]:
        self.applications.require_record(application_id)
        return [
            ConversationResponse.model_validate(item)
            for item in self.repository.list_conversations(application_id, channel)
        ]

    def messages(
        self, application_id: str, conversation_id: str
    ) -> list[ConversationMessageResponse]:
        self.applications.require_record(application_id)
        items = self.repository.get_messages(application_id, conversation_id)
        if items is None:
            raise ApiError("会话不存在", status.HTTP_404_NOT_FOUND)
        result = []
        for item in items:
            images = (
                self.image_service.console_images(
                    application_id, conversation_id, item["id"]
                )
                if self.image_service is not None
                else []
            )
            item["images"] = images
            item["sources"] = [
                self._source_projection(source, images) for source in item["sources"]
            ]
            result.append(ConversationMessageResponse.model_validate(item))
        return result

    def source(
        self,
        application_id: str,
        conversation_id: str,
        message_id: str,
        source_id: str,
    ) -> MessageSourceResponse:
        self.applications.require_record(application_id)
        item = self.repository.get_source(
            application_id,
            conversation_id,
            message_id,
            source_id,
        )
        if item is None:
            raise ApiError("检索片段不存在", status.HTTP_404_NOT_FOUND)
        images = (
            self.image_service.console_images(
                application_id, conversation_id, message_id
            )
            if self.image_service is not None
            else []
        )
        return MessageSourceResponse.model_validate(
            self._source_projection(item, images)
        )

    def delete_conversation(self, application_id: str, conversation_id: str) -> None:
        self.applications.require_record(application_id)
        lock = self._conversation_lock(f"{application_id}:{conversation_id}")
        with lock:
            if not self.repository.delete_conversation(application_id, conversation_id):
                raise ApiError("会话不存在", status.HTTP_404_NOT_FOUND)

    def _mark_failed(
        self,
        application_id: str,
        conversation_id: str,
        assistant_message_id: str,
        error: Exception,
    ) -> None:
        try:
            self.repository.fail_turn(
                application_id=application_id,
                conversation_id=conversation_id,
                assistant_message_id=assistant_message_id,
                error_message=str(error) or error.__class__.__name__,
                now=_now(),
            )
        except Exception:
            # Preserve the original model/API error if failure bookkeeping also fails,
            # but leave an audit trail for operators to repair the pending row.
            logger.exception(
                "chat.persistence_failure application_id=%s conversation_id=%s "
                "assistant_message_id=%s",
                application_id,
                conversation_id,
                assistant_message_id,
            )

    def shutdown(self) -> None:
        self._stream_executor.shutdown(wait=True, cancel_futures=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["ChatService"]
