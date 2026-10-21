"""Public invocation admission, bounded execution, and durable result replay."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import queue
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlencode

from langgraph.checkpoint.sqlite import SqliteSaver

from app.api_server.apis.v1.runtime_schemas import (
    PublicChatRequest,
    PublicChatResponse,
    PublicClarification,
    PublicRunResponse,
    PublicSnippet,
)
from app.api_server.common.errors import ApiError
from app.api_server.config import ApiServerSettings
from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.repositories.application_runs import ApplicationRunRepository
from app.api_server.services.application_release_service import (
    ApiPrincipal,
    ApplicationReleaseService,
)
from app.api_server.repositories.wecom_channels import WecomChannelRepository
from app.api_server.services.application_service import ApplicationService
from nianlun.agent.lead_agent.runtime import iter_agent_stream_events
from nianlun.agent.middleware import HistoryOverCapacityError
from nianlun.agent.retrieval_answer.input_messages import validate_public_messages

logger = logging.getLogger(__name__)

_CHANNEL_STREAM_END = object()
_CHANNEL_MAX_MESSAGE_BYTES = 64 * 1024


def checkpoint_thread_id(channel: str, conversation_id: str) -> str:
    if channel not in {"api", "wecom"}:
        raise ValueError("unsupported conversation channel")
    return f"{channel}:{conversation_id}"


def _public_clarification(raw: Any) -> PublicClarification | None:
    """Project a raw clarification payload onto the public whitelist."""
    if not isinstance(raw, dict):
        return None
    return PublicClarification(
        clarification_id=str(raw.get("clarification_id", "")),
        clarification_type=str(raw.get("clarification_type", "")),
        status=str(raw.get("status", "")),
        question=str(raw.get("question", "")),
        context=raw.get("context"),
        options=list(raw.get("options") or []),
        duplicate=bool(raw.get("duplicate", False)),
    )


def _is_over_capacity_error(exc: BaseException) -> bool:
    """Detect the typed capacity error through LangGraph's exception wrapping."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, HistoryOverCapacityError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


@dataclass
class PreparedInvocation:
    row: dict[str, Any]
    future: Future[PublicChatResponse] | None = None
    cancellation: threading.Event | None = None
    started: float = 0.0
    replay: PublicChatResponse | None = None
    deltas: queue.Queue[Any] | None = None
    channel: str = "api"
    timeout_seconds: float = 300.0
    retention_days: int = 30
    duplicate: bool = False


def public_run(
    row: dict[str, Any], *, include_result: bool = True
) -> PublicRunResponse:
    result = (
        PublicChatResponse.model_validate_json(row["result_json"])
        if row["result_json"]
        else None
    )
    finished = (
        datetime.fromisoformat(row["finished_at"])
        if row["finished_at"]
        else datetime.now(timezone.utc)
    )
    duration_ms = max(
        0,
        int(
            (finished - datetime.fromisoformat(row["created_at"])).total_seconds()
            * 1000
        ),
    )
    return PublicRunResponse(
        run_id=row["id"],
        app_id=row["application_id"],
        client_id=row["client_id"],
        release_id=row["release_id"],
        conversation_id=row["conversation_id"],
        status=row["status"],
        error_type=row["error_type"],
        error_status=row["error_status"],
        error_message=row["error_message"],
        result=result if include_result else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        knowledge_base_content_version=row["knowledge_base_content_version"],
        request_id=row["request_id"],
        duration_ms=duration_ms,
        usage=result.usage if result else None,
    )


class ApplicationInvocationService:
    def __init__(
        self,
        publishing: ApplicationReleaseService,
        applications: ApplicationService,
        factory: SQLiteConnectionFactory,
        settings: ApiServerSettings,
    ) -> None:
        self.publishing, self.applications, self.settings = (
            publishing,
            applications,
            settings,
        )
        self.repository = ApplicationRunRepository(factory)
        self.channels = WecomChannelRepository(factory)
        self._guard = threading.RLock()
        self._active: dict[str, tuple[str, str, str, PreparedInvocation]] = {}
        self._rates: dict[str, deque[float]] = {}
        self._closed = False
        self._next_cleanup = time.monotonic() + 60
        self._executor = ThreadPoolExecutor(
            max_workers=settings.application_api_max_concurrent
            + settings.wecom_max_concurrent,
            thread_name_prefix="nianlun-public",
        )
        self._checkpoint_connection: sqlite3.Connection | None = None
        self.checkpointer: SqliteSaver | None = None
        if settings.application_api_enabled or settings.internal_token is not None:
            path = settings.data_dir / "application_checkpoints.sqlite3"
            missing = not path.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._checkpoint_connection = sqlite3.connect(path, check_same_thread=False)
            self.checkpointer = SqliteSaver(self._checkpoint_connection)
            self.checkpointer.setup()
            self.repository.recover(checkpoint_missing=missing)
            self.cleanup_expired_checkpoints()

    def authenticate(
        self, application_id: str | None, authorization: str | None
    ) -> ApiPrincipal:
        if not self.settings.application_api_enabled:
            raise ApiError("公开应用 API 未启用", 404)
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or token.strip() != token:
            raise ApiError("无效的应用凭据", 401)
        return self.publishing.authenticate(application_id, token)

    @staticmethod
    def _fingerprint(
        body: PublicChatRequest, input_messages: list[dict[str, str]] | None = None
    ) -> str:
        payload: dict[str, Any] = {
            "message": body.message,
            "user_id": body.user_id,
            "conversation_id": body.conversation_id,
            "response_mode": body.response_mode,
            "protocol_version": 1,
        }
        if input_messages is not None:
            payload["protocol_version"] = "openai-1"
            payload["messages"] = input_messages
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    @staticmethod
    def _replay(row: dict[str, Any], fingerprint: str) -> PreparedInvocation:
        location = ApplicationInvocationService.location(row)
        if row["request_fingerprint"] != fingerprint:
            raise ApiError("幂等键已用于其他请求", 409, headers={"Location": location})
        if row["status"] == "completed":
            return PreparedInvocation(
                row=row,
                replay=PublicChatResponse.model_validate_json(row["result_json"]),
            )
        if row["status"] in ("running", "cancelling"):
            raise ApiError(
                "调用正在执行", 409, headers={"Location": location, "Retry-After": "1"}
            )
        raise ApiError(
            row["error_message"] or "调用已终止",
            row["error_status"] or 409,
            headers={"Location": location},
        )

    def prepare(
        self,
        principal: ApiPrincipal,
        body: PublicChatRequest,
        key: str,
        request_id: str | None = None,
        *,
        input_messages: list[dict[str, str]] | None = None,
        stream_output: bool = False,
    ) -> PreparedInvocation:
        if not key.strip() or len(key) > 128:
            raise ApiError("Idempotency-Key 必填且最多 128 字符", 422)
        app_id, client_id = principal.application_id, principal.client_id
        fingerprint = self._fingerprint(body, input_messages)
        with self.publishing.entry_lock(app_id):
            with self._guard:
                if self._closed or self.checkpointer is None:
                    raise ApiError("运行服务不可用", 503)
                if time.monotonic() >= self._next_cleanup:
                    self.cleanup_expired_checkpoints()
                    self._next_cleanup = time.monotonic() + 60
                self.publishing.validate_principal(principal)
                existing = self.repository.find(app_id, client_id, body.user_id, key)
                if existing:
                    return self._replay(existing, fingerprint)
                release = self.publishing.runtime_release(principal)
                if input_messages is not None:
                    try:
                        validate_public_messages(
                            input_messages,
                            agent_version=release.config.agent_version,
                            context_window_tokens=release.config.context_window_tokens,
                        )
                    except ValueError:
                        raise ApiError(
                            "完整 messages 超出 Agent 上下文限制", 400
                        ) from None
                if body.conversation_id:
                    conversation = self.repository.conversation(
                        app_id, client_id, body.user_id, body.conversation_id
                    )
                    release = self.publishing.runtime_release(
                        principal, conversation["release_id"]
                    )
                    checkpoint = self.checkpointer.get_tuple(
                        {
                            "configurable": {
                                "thread_id": checkpoint_thread_id(
                                    "api", body.conversation_id
                                ),
                                "checkpoint_ns": "",
                            }
                        }
                    )
                    if checkpoint is None:
                        self.repository.interrupt_context(body.conversation_id)
                        raise ApiError("会话 checkpoint 缺失，请创建新会话", 409)
                now = time.monotonic()
                if self._limits_exceeded(app_id, client_id, now):
                    raise ApiError(
                        "应用调用超过限额", 429, headers={"Retry-After": "1"}
                    )
            # Building the runtime can reach Milvus/network. Keep it out of the
            # process-global admission guard so one app cannot stall every other
            # app's admission; ``entry_lock`` still serializes this app.
            record = self.applications.knowledge_base_lookup(
                release.config.knowledge_base_id
            )
            runtime = self.applications.release_runtime(
                release.config, self.checkpointer, record
            )
            prepared: PreparedInvocation | None = None
            try:
                with self._guard:
                    # Fresh timestamp: the runtime build above may have blocked.
                    admitted_at = time.monotonic()
                    if self._limits_exceeded(app_id, client_id, admitted_at):
                        raise ApiError(
                            "应用调用超过限额", 429, headers={"Retry-After": "1"}
                        )
                    row = self.repository.admit(
                        app_id=app_id,
                        client_id=client_id,
                        user_id=body.user_id,
                        release_id=release.id,
                        conversation_id=body.conversation_id,
                        message=body.message,
                        key=key,
                        fingerprint=fingerprint,
                        content_version=int(record.get("content_version", 0)),
                        request_id=request_id,
                        retention_days=self.settings.application_api_retention_days,
                        key_id=principal.key_id,
                    )
                    prepared = PreparedInvocation(
                        row=row,
                        cancellation=threading.Event(),
                        started=admitted_at,
                        deltas=queue.Queue(maxsize=256) if stream_output else None,
                        channel="api",
                        timeout_seconds=self.settings.application_api_timeout_seconds,
                        retention_days=self.settings.application_api_retention_days,
                    )
                    self._active[row["id"]] = (app_id, client_id, "api", prepared)
                    prepared.future = self._executor.submit(
                        self._execute,
                        prepared,
                        runtime,
                        body.message,
                        release.config.clarification_enabled,
                        input_messages,
                    )
                    # Only count the request once it is actually scheduled; a failed
                    # submit must not consume the caller's per-minute budget.
                    self._rates.setdefault(client_id, deque()).append(admitted_at)
                    return prepared
            except BaseException:
                if prepared is not None:
                    with self._guard:
                        self._active.pop(prepared.row["id"], None)
                    # Do not leave an admitted run stuck in "running" until restart.
                    try:
                        self.repository.cancel(
                            prepared.row["id"],
                            "failed",
                            502,
                            "调用未能启动",
                            exited=True,
                        )
                    except Exception:
                        logger.warning(
                            "application.submit_failure_cleanup_failed run_id=%s",
                            prepared.row["id"],
                            exc_info=True,
                        )
                self.applications.release_runtime_resources(runtime)
                raise

    @staticmethod
    def _channel_fingerprint(user_key: str, text: str) -> str:
        payload = {
            "message": text,
            "user_key": user_key,
            "protocol_version": 1,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    @staticmethod
    def _valid_user_key(user_key: str) -> bool:
        return (
            len(user_key) == 35
            and user_key.startswith("wc_")
            and all(character in "0123456789abcdef" for character in user_key[3:])
        )

    def _channel_record(self, channel_id: str) -> dict[str, Any]:
        channel = self.channels.by_channel_id(channel_id)
        if channel is None:
            raise ApiError("渠道不存在", 404)
        return channel

    @staticmethod
    def _validate_channel(channel: dict[str, Any], revision: int) -> None:
        if not channel["enabled"]:
            raise ApiError("企业微信渠道已停用", 403)
        expires_at = channel["expires_at"]
        if expires_at and datetime.fromisoformat(expires_at) <= datetime.now(
            timezone.utc
        ):
            raise ApiError("企业微信渠道已过期", 403)
        if channel["config_revision"] != revision:
            raise ApiError("渠道配置已变化，请重试", 409)

    @staticmethod
    def _conversation_usable(
        conversation: dict[str, Any], checkpointer: SqliteSaver
    ) -> bool:
        expires_at = conversation["expires_at"]
        if conversation["context_state"] != "ready":
            return False
        if expires_at and datetime.fromisoformat(expires_at) <= datetime.now(
            timezone.utc
        ):
            return False
        return (
            checkpointer.get_tuple(
                {
                    "configurable": {
                        "thread_id": checkpoint_thread_id("wecom", conversation["id"]),
                        "checkpoint_ns": "",
                    }
                }
            )
            is not None
        )

    def prepare_channel(
        self,
        channel_id: str,
        revision: int,
        user_key: str,
        text: str,
        key: str,
        request_id: str | None,
        started: float,
    ) -> PreparedInvocation:
        if not key.strip() or len(key) > 128:
            raise ApiError("Idempotency-Key 必填且最多 128 字符", 422)
        if not self._valid_user_key(user_key):
            raise ApiError("会话标识无效", 422)
        message = text.strip()
        if not message:
            raise ApiError("消息不能为空", 422)
        if len(message.encode("utf-8")) > _CHANNEL_MAX_MESSAGE_BYTES:
            raise ApiError("消息过长", 413)
        channel = self._channel_record(channel_id)
        app_id = channel["application_id"]
        client_id = f"wecom:{channel_id}"
        fingerprint = self._channel_fingerprint(user_key, message)
        timeout = self.settings.wecom_run_timeout_seconds
        with self.publishing.entry_lock(app_id):
            with self._guard:
                if self._closed or self.checkpointer is None:
                    raise ApiError("运行服务不可用", 503)
                if time.monotonic() >= self._next_cleanup:
                    self.cleanup_expired_checkpoints()
                    self._next_cleanup = time.monotonic() + 60
                channel = self._channel_record(channel_id)
                if channel["application_id"] != app_id:
                    raise ApiError("渠道不存在", 404)
                self._validate_channel(channel, revision)
                existing = self.repository.find(app_id, client_id, user_key, key)
                if existing:
                    prepared = self._replay(existing, fingerprint)
                    prepared.channel = "wecom"
                    prepared.timeout_seconds = timeout
                    prepared.retention_days = (
                        self.settings.wecom_conversation_retention_days
                    )
                    prepared.duplicate = prepared.replay is not None
                    return prepared
                conversation = self.repository.latest_channel_conversation(
                    app_id, user_key
                )
                conversation_id: str | None = None
                release = None
                if conversation is not None:
                    if self.repository.has_active_run(conversation["id"]):
                        raise ApiError("会话正忙", 409)
                    usable = self._conversation_usable(conversation, self.checkpointer)
                    if usable:
                        try:
                            release = self.publishing.require_release(
                                app_id, conversation["release_id"]
                            )
                        except ApiError:
                            usable = False
                    if usable:
                        conversation_id = conversation["id"]
                    else:
                        self.repository.interrupt_context(conversation["id"])
                        self.repository.archive_conversation(conversation["id"])
                if conversation_id is None:
                    release_id = channel["active_release_id"]
                    if not release_id:
                        raise ApiError("应用尚未发布", 403)
                    release = self.publishing.require_release(app_id, release_id)
                assert release is not None
                self.publishing.validate_release(release.config)
                knowledge = self.applications.knowledge_base_lookup(
                    release.config.knowledge_base_id
                )
                if not knowledge.get("snapshot_relpath") or not knowledge.get(
                    "snapshot_manifest_sha256"
                ):
                    raise ApiError("知识库 committed snapshot 尚未就绪", 503)
            record = self.applications.knowledge_base_lookup(
                release.config.knowledge_base_id
            )
            runtime = self.applications.release_runtime(
                release.config, self.checkpointer, record
            )
            prepared: PreparedInvocation | None = None
            try:
                if time.monotonic() - started >= timeout:
                    raise ApiError("调用超时", 504)
                with self._guard:
                    if self._closed:
                        raise ApiError("运行服务不可用", 503)
                    if self._wecom_limits_exceeded():
                        raise ApiError(
                            "企业微信调用超过并发上限",
                            429,
                            headers={"Retry-After": "1"},
                        )
                    row = self.repository.admit_channel(
                        app_id=app_id,
                        channel_id=channel_id,
                        revision=revision,
                        user_key=user_key,
                        release_id=release.id,
                        conversation_id=conversation_id,
                        message=message,
                        key=key,
                        fingerprint=fingerprint,
                        content_version=int(record.get("content_version", 0)),
                        request_id=request_id,
                        retention_days=self.settings.wecom_conversation_retention_days,
                    )
                    prepared = PreparedInvocation(
                        row=row,
                        cancellation=threading.Event(),
                        started=started,
                        deltas=queue.Queue(maxsize=256),
                        channel="wecom",
                        timeout_seconds=timeout,
                        retention_days=self.settings.wecom_conversation_retention_days,
                    )
                    self._active[row["id"]] = (
                        app_id,
                        client_id,
                        "wecom",
                        prepared,
                    )
                    prepared.future = self._executor.submit(
                        self._execute,
                        prepared,
                        runtime,
                        message,
                        release.config.clarification_enabled,
                        None,
                    )
                    return prepared
            except BaseException:
                if prepared is not None:
                    with self._guard:
                        self._active.pop(prepared.row["id"], None)
                    try:
                        self.repository.cancel(
                            prepared.row["id"],
                            "failed",
                            502,
                            "调用未能启动",
                            exited=True,
                        )
                    except Exception:
                        logger.warning(
                            "application.submit_failure_cleanup_failed run_id=%s",
                            prepared.row["id"],
                            exc_info=True,
                        )
                self.applications.release_runtime_resources(runtime)
                raise

    async def stream_channel(
        self, prepared: PreparedInvocation
    ) -> AsyncIterator[dict[str, Any]]:
        if prepared.duplicate:
            return
        identity = {
            "run_id": prepared.row["id"],
            "conversation_id": prepared.row["conversation_id"],
            "release_id": prepared.row["release_id"],
        }
        if prepared.replay is not None:
            yield {"type": "start", **identity}
            yield {
                "type": "done",
                "content": prepared.replay.answer,
                "run_id": prepared.row["id"],
            }
            return
        task = asyncio.create_task(self.wait(prepared))
        try:
            yield {"type": "start", **identity}
            while not task.done() or (
                prepared.deltas is not None and not prepared.deltas.empty()
            ):
                if prepared.deltas is not None:
                    try:
                        item = prepared.deltas.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        if item is _CHANNEL_STREAM_END:
                            break
                        yield {"type": "chunk", "content": item}
                        continue
                await asyncio.sleep(0.01)
            result = await task
            yield {"type": "done", "content": result.answer, "run_id": result.run_id}
        except ApiError as exc:
            yield {
                "type": "error",
                "code": exc.status_code,
                "message": exc.message,
            }
        except Exception:
            logger.warning(
                "application.channel_stream_failed run_id=%s",
                prepared.row["id"],
                exc_info=True,
            )
            yield {
                "type": "error",
                "code": 500,
                "message": "模型调用失败",
            }
        finally:
            if not task.done():
                self.cancel(prepared)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _limits_exceeded(self, app_id: str, client_id: str, now: float) -> bool:
        """Prune the rate window and report whether admission would exceed limits.

        Caller must hold ``self._guard``; the window and the active set are shared.
        """
        window = self._rates.setdefault(client_id, deque())
        while window and window[0] <= now - 60:
            window.popleft()
        if not window:
            self._rates.pop(client_id, None)
        active = [item for item in self._active.values() if item[2] == "api"]
        return (
            len(active) >= self.settings.application_api_max_concurrent
            or sum(item[0] == app_id for item in active)
            >= self.settings.application_api_app_concurrent
            or sum(item[1] == client_id for item in active)
            >= self.settings.application_api_client_concurrent
            or len(window) >= self.settings.application_api_requests_per_minute
        )

    def _wecom_limits_exceeded(self) -> bool:
        """WeCom runs have their own global budget and no per-caller RPM window."""
        return (
            sum(item[2] == "wecom" for item in self._active.values())
            >= self.settings.wecom_max_concurrent
        )

    def _execute(
        self,
        prepared: PreparedInvocation,
        runtime: Any,
        message: str,
        clarification_enabled: bool,
        input_messages: list[dict[str, str]] | None = None,
    ) -> PublicChatResponse:
        row = prepared.row
        thread_id = checkpoint_thread_id(prepared.channel, row["conversation_id"])
        try:
            raw = None
            events = (
                runtime.iter_public_events(
                    input_messages,
                    thread_id=thread_id,
                    clarification_enabled=clarification_enabled,
                    cancellation_event=prepared.cancellation,
                )
                if input_messages is not None
                else iter_agent_stream_events(
                    runtime,
                    message,
                    thread_id=thread_id,
                    clarification_enabled=clarification_enabled,
                    cancellation_event=prepared.cancellation,
                )
            )
            try:
                for event in events:
                    if event.get("type") == "done":
                        raw = event["data"]
                    elif event.get("type") == "message" and prepared.deltas is not None:
                        text = str(event["data"]["delta"])
                        # Bound both queue slots and per-slot memory; cancellation releases backpressure.
                        for offset in range(0, len(text), 4096):
                            while True:
                                if (
                                    prepared.cancellation is not None
                                    and prepared.cancellation.is_set()
                                ):
                                    raise ApiError("调用已取消", 409)
                                try:
                                    prepared.deltas.put(
                                        text[offset : offset + 4096], timeout=0.1
                                    )
                                    break
                                except queue.Full:
                                    continue
            finally:
                close_events = getattr(events, "close", None)
                if callable(close_events):
                    close_events()
            if prepared.cancellation is not None and prepared.cancellation.is_set():
                raise ApiError("调用已取消", 409)
            if raw is None:
                raise ApiError("模型未返回最终结果", 502)
            response = PublicChatResponse(
                app_id=row["application_id"],
                release_id=row["release_id"],
                run_id=row["id"],
                conversation_id=row["conversation_id"],
                message_id=row["message_id"],
                answer=raw["answer"],
                retrieved_snippets=[
                    PublicSnippet.model_validate(item)
                    for item in raw.get("retrieved_snippets", [])
                ],
                usage=raw.get("usage"),
                ttft_ms=raw.get("ttft_ms"),
                clarification=_public_clarification(raw.get("clarification")),
                knowledge_base_content_version=row["knowledge_base_content_version"],
            )
            if not self.repository.complete(
                row["id"],
                response.model_dump(mode="json"),
                raw,
                prepared.retention_days,
            ):
                raise ApiError("调用已终止", 409)
            return response
        except Exception as exc:
            current = self.repository.get(row["id"])
            if current and current["status"] == "cancelling":
                self.repository.cancel(
                    row["id"],
                    current["error_type"] or "cancelled",
                    current["error_status"] or 409,
                    current["error_message"] or "调用已取消",
                    exited=True,
                )
            else:
                if _is_over_capacity_error(exc):
                    self.repository.cancel(
                        row["id"],
                        "failed",
                        400,
                        "请求历史超出模型上下文容量",
                        exited=True,
                    )
                else:
                    self.repository.cancel(
                        row["id"], "failed", 502, "模型调用失败", exited=True
                    )
            logger.warning(
                "application.run_failed run_id=%s error_class=%s",
                row["id"],
                type(exc).__name__,
            )
            terminal = self.repository.get(row["id"])
            if terminal is None:
                raise ApiError(
                    "模型调用失败", 502, headers={"Location": self.location(row)}
                ) from None
            raise ApiError(
                terminal["error_message"] or "模型调用失败",
                terminal["error_status"] or 502,
                headers={"Location": self.location(row)},
            ) from None
        finally:
            try:
                self.applications.release_runtime_resources(runtime)
            finally:
                try:
                    with self._guard:
                        self._active.pop(row["id"], None)
                    if input_messages is not None and self.checkpointer is not None:
                        # Caller-owned transcripts are stateless: reclaim the thread so
                        # repeated calls cannot accumulate checkpoints on disk.
                        try:
                            self.checkpointer.delete_thread(thread_id)
                        except sqlite3.Error:
                            logger.warning(
                                "application.ephemeral_checkpoint_cleanup_failed conversation_id=%s",
                                row["conversation_id"],
                            )
                finally:
                    # Channel streaming waits on this marker instead of polling the
                    # shared queue; public SSE consumers never receive it.
                    if prepared.channel == "wecom" and prepared.deltas is not None:
                        try:
                            prepared.deltas.put_nowait(_CHANNEL_STREAM_END)
                        except queue.Full:
                            pass

    def cancel(self, prepared: PreparedInvocation, *, timed_out: bool = False) -> None:
        if prepared.cancellation is not None:
            try:
                self.repository.cancel(
                    prepared.row["id"],
                    "timed_out" if timed_out else "cancelled",
                    504 if timed_out else 409,
                    "调用超时" if timed_out else "调用已取消",
                    exited=False,
                )
            finally:
                # Persist the reason before waking a worker that may exit immediately.
                prepared.cancellation.set()

    async def wait(self, prepared: PreparedInvocation) -> PublicChatResponse:
        if prepared.replay is not None:
            return prepared.replay
        assert prepared.future is not None
        remaining = prepared.timeout_seconds - (time.monotonic() - prepared.started)
        future = asyncio.wrap_future(prepared.future)

        def observe_completion(completed: asyncio.Future[PublicChatResponse]) -> None:
            if not completed.cancelled():
                completed.exception()

        # The HTTP waiter can finish before the worker; observe late terminal errors.
        future.add_done_callback(observe_completion)
        try:
            return await asyncio.wait_for(asyncio.shield(future), max(0, remaining))
        except TimeoutError:
            self.cancel(prepared, timed_out=True)
            raise ApiError(
                "调用超时", 504, headers={"Location": self.location(prepared.row)}
            ) from None
        except asyncio.CancelledError:
            self.cancel(prepared)
            raise

    def get(
        self, principal: ApiPrincipal, run_id: str, user_id: str
    ) -> PublicRunResponse:
        self.publishing.validate_principal(principal)
        row = self.repository.get(run_id)
        if (
            row is None
            or row["application_id"] != principal.application_id
            or row["client_id"] != principal.client_id
            or row["user_id"] != user_id
        ):
            raise ApiError("调用不存在", 404)
        return public_run(row)

    @staticmethod
    def location(row: dict[str, Any]) -> str:
        return f"/api/v1/runtime/apps/{row['application_id']}/runs/{row['id']}?{urlencode({'user_id': row['user_id']})}"

    def list(self, application_id: str, **filters: Any) -> dict[str, Any]:
        self.applications.require_record(application_id)
        rows = self.repository.list(application_id, **filters)
        return {
            "items": [
                public_run(row, include_result=False).model_dump(mode="json")
                for row in rows
            ],
            "total": self.repository.count(
                application_id,
                status=filters.get("status"),
                client_id=filters.get("client_id"),
            ),
        }

    def delete_application(self, application_id: str) -> None:
        """Delete an application and reclaim its API checkpoint threads.

        Conversation ids are collected before the cascade removes them, because
        checkpoints live in a separate SQLite file the application cascade
        cannot reach.
        """
        conversation_ids = self.repository.conversation_ids_for_application(
            application_id
        )
        self.applications.delete(application_id)
        if self.checkpointer is None:
            return
        for conversation_id, channel in conversation_ids:
            try:
                self.checkpointer.delete_thread(
                    checkpoint_thread_id(channel, conversation_id)
                )
            except sqlite3.Error:
                logger.warning(
                    "application.checkpoint_cleanup_failed conversation_id=%s",
                    conversation_id,
                )

    def cleanup_expired_checkpoints(self) -> None:
        if self.checkpointer is None:
            return
        with self._guard:
            for (
                conversation_id,
                channel,
            ) in self.repository.checkpoint_cleanup_candidates():
                try:
                    self.checkpointer.delete_thread(
                        checkpoint_thread_id(channel, conversation_id)
                    )
                except sqlite3.Error:
                    logger.warning(
                        "application.checkpoint_cleanup_failed conversation_id=%s",
                        conversation_id,
                    )
                else:
                    self.repository.checkpoint_cleaned(conversation_id)
            cutoff = time.monotonic() - 60
            for client_id, window in list(self._rates.items()):
                while window and window[0] <= cutoff:
                    window.popleft()
                if not window:
                    self._rates.pop(client_id, None)

    def close(self) -> None:
        with self._guard:
            self._closed = True
            pending = [item[3] for item in self._active.values()]
        for prepared in pending:
            self.cancel(prepared)
        self._executor.shutdown(wait=True)
        if self._checkpoint_connection is not None:
            self._checkpoint_connection.close()
