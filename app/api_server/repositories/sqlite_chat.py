"""SQLAlchemy persistence for conversations, messages, and retrieved text."""

from __future__ import annotations

from nianlun.agent.contracts import AnswerImagePolicy

import json
import logging
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from pydantic import ValidationError
from nianlun.agent.image_history import (
    HistoryImage,
    HistoryImageSource,
    HistoryTurn,
    TrustedHistory,
    fit_history,
)

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import (
    Conversation,
    Message,
    MessageImage,
    MessageSource,
    ApplicationRun,
    AnswerImageBlob,
)
from app.api_server.repositories.answer_images import (
    commit_image_blob,
    valid_source_bindings,
    normalize_completion_images,
)

logger = logging.getLogger(__name__)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _source_text(source: dict[str, Any]) -> str:
    for key in ("text", "content", "snippet", "page_content"):
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _parse_json_list(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _parse_json_dict(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _doc_name_from_metadata(metadata_json: Any) -> str | None:
    """Recover ``doc_name`` from the persisted source metadata blob."""
    if not metadata_json:
        return None
    try:
        data = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict):
        name = data.get("doc_name")
        if isinstance(name, str) and name.strip():
            return name
    return None


def _citation_id_from_metadata(metadata_json: Any, source_order: int) -> int:
    """Recover the turn-local citation number, with legacy-row fallback."""
    if metadata_json:
        try:
            data = json.loads(metadata_json)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            citation_id = _optional_int(data.get("citation_id"))
            if citation_id is not None and citation_id > 0:
                return citation_id
    return source_order + 1


def _message_dict(item: Message) -> dict[str, Any]:
    result = {
        "id": item.id,
        "conversation_id": item.conversation_id,
        "seq_no": item.seq_no,
        "role": item.role,
        "content": item.content,
        "status": item.status,
        "route": item.route,
        "error_message": item.error_message,
        "tool_calls": _parse_json_list(item.tool_calls_json),
        "trace": _parse_json_list(item.trace_json),
        "usage": _parse_json_dict(item.usage_json),
        "ttft_ms": item.ttft_ms,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "sources": [],
        "images": [],
        "answer_format_version": item.answer_format_version,
        "answer_output_mode": item.answer_output_mode,
        "image_previews_unavailable": item.image_previews_unavailable,
    }
    return result


def _source_dict(item: MessageSource) -> dict[str, Any]:
    metadata = _parse_json_dict(item.metadata_json) or {}
    return {
        "id": item.id,
        "message_id": item.message_id,
        "source_order": item.source_order,
        "citation_id": _citation_id_from_metadata(
            item.metadata_json, item.source_order
        ),
        "doc_id": item.doc_id,
        "node_id": item.node_id,
        "line_spec": item.line_spec,
        "line_num": item.line_num,
        "title": item.title,
        "text": item.retrieved_text,
        "char_offset": item.char_offset,
        "char_limit": item.char_limit,
        "total_chars": item.total_chars,
        "text_truncated": bool(item.text_truncated),
        "content_version": item.content_version,
        "doc_name": _doc_name_from_metadata(item.metadata_json),
        "images": metadata.get("images", [])
        if isinstance(metadata.get("images", []), list)
        else [],
    }


class SQLiteChatRepository:
    """Persist product-facing chat history independently from Agent state."""

    def __init__(self, factory: SQLiteConnectionFactory) -> None:
        self.factory = factory

    def trusted_history(
        self,
        application_id: str,
        conversation_id: str,
        *,
        channel: str = "console",
        client_id: str | None = None,
        user_id: str | None = None,
    ) -> TrustedHistory:
        """Load committed turns within the complete owner boundary."""
        with self.factory.session_scope() as session:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.application_id == application_id,
                    Conversation.channel == channel,
                    Conversation.api_client_id == client_id,
                    Conversation.external_user_id == user_id,
                    Conversation.status != "deleted",
                )
            )
            if conversation is None:
                return TrustedHistory()
            query = select(Message).where(
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
                Message.status == "completed",
            )
            if channel == "api":
                query = query.join(
                    ApplicationRun, ApplicationRun.message_id == Message.id
                ).where(
                    ApplicationRun.application_id == application_id,
                    ApplicationRun.conversation_id == conversation_id,
                    ApplicationRun.client_id == client_id,
                    ApplicationRun.user_id == user_id,
                    ApplicationRun.status == "completed",
                )
            assistants = session.scalars(
                query.order_by(Message.seq_no.desc()).limit(3)
            ).all()
            turns = []
            now = datetime.now(timezone.utc)
            for assistant in reversed(assistants):
                image_result_valid = True
                if channel == "api":
                    run = session.scalar(
                        select(ApplicationRun).where(
                            ApplicationRun.message_id == assistant.id,
                            ApplicationRun.application_id == application_id,
                            ApplicationRun.conversation_id == conversation_id,
                            ApplicationRun.client_id == client_id,
                            ApplicationRun.user_id == user_id,
                            ApplicationRun.status == "completed",
                        )
                    )
                    image_result_valid = bool(
                        run is not None
                        and run.result_expires_at
                        and datetime.fromisoformat(run.result_expires_at) > now
                    )
                user = session.scalar(
                    select(Message).where(
                        Message.conversation_id == conversation_id,
                        Message.seq_no == assistant.seq_no - 1,
                        Message.role == "user",
                        Message.status == "completed",
                    )
                )
                if user is None:
                    continue
                image_query = select(MessageImage).where(
                    MessageImage.message_id == assistant.id,
                    MessageImage.display_in_answer.is_(True),
                )
                count = (
                    session.scalar(
                        select(func.count()).select_from(image_query.subquery())
                    )
                    or 0
                )
                attachments = session.scalars(
                    image_query.order_by(MessageImage.sequence).limit(64)
                ).all()
                images = []
                for order, attachment in enumerate(attachments, 1):
                    bindings = valid_source_bindings(
                        session, json.loads(attachment.source_bindings_json)
                    )
                    sources = []
                    for binding in bindings:
                        try:
                            source = HistoryImageSource.model_validate(
                                {
                                    key: value
                                    for key, value in binding.items()
                                    if key in HistoryImageSource.model_fields
                                }
                            )
                        except ValidationError:
                            logger.warning(
                                "chat.image_history_source_unavailable reason=invalid_identity message_id=%s",
                                assistant.id,
                            )
                            continue
                        sources.append(source)
                    blob = (
                        session.get(AnswerImageBlob, attachment.blob_id)
                        if attachment.blob_id
                        else None
                    )
                    images.append(
                        HistoryImage(
                            order=order,
                            caption=attachment.caption[:256],
                            status="available"
                            if attachment.status == "available"
                            and image_result_valid
                            and sources
                            and blob is not None
                            and blob.status == "ready"
                            else "unavailable",
                            sources=tuple(sources[:4]),
                            sources_truncated=len(sources) > 4,
                        )
                    )
                turns.append(
                    HistoryTurn.model_validate(
                        {
                            "message_id": assistant.id,
                            "question": user.content,
                            "answer": assistant.content,
                            "answer_output_mode": assistant.answer_output_mode,
                            "images": tuple(images),
                            "image_count": count,
                            "images_truncated": count > len(images),
                        }
                    )
                )
            return fit_history(TrustedHistory(turns=tuple(turns)))

    def fail_pending_messages(self, now: datetime) -> int:
        """Close turns left pending by a previous API-server process."""
        timestamp = now.isoformat()
        with self.factory.session_scope(write=True) as session:
            pending = session.scalars(
                select(Message).where(Message.status == "pending")
            ).all()
            conversation_ids: set[str] = set()
            for message in pending:
                message.status = "failed"
                if message.error_message is None:
                    message.error_message = "服务进程重启，流式回答未完成"
                message.updated_at = timestamp
                conversation_ids.add(message.conversation_id)
            for conversation_id in conversation_ids:
                conversation = session.get(Conversation, conversation_id)
                if conversation is not None:
                    conversation.updated_at = timestamp
            return len(pending)

    def begin_turn(
        self,
        *,
        application_id: str,
        conversation_id: str,
        user_message_id: str,
        assistant_message_id: str,
        user_content: str,
        now: datetime,
        session: Session | None = None,
        channel: str = "console",
    ) -> str | None:
        timestamp = now.isoformat()
        with (
            nullcontext(session)
            if session is not None
            else self.factory.session_scope(write=True)
        ) as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                from nianlun.agent.prompt_registry import CURRENT_PROMPT_BUNDLE

                conversation = Conversation(
                    id=conversation_id,
                    application_id=application_id,
                    title=user_content[:40],
                    status="active",
                    created_at=timestamp,
                    updated_at=timestamp,
                    last_message_at=timestamp,
                    agent_contract_json=CURRENT_PROMPT_BUNDLE.contract_json()
                    if channel == "console"
                    else None,
                )
                session.add(conversation)
                next_seq = 1
            else:
                if conversation.channel != channel:
                    raise ValueError("会话不存在")
                if conversation.application_id != application_id:
                    raise ValueError("会话不属于当前应用")
                if conversation.status == "deleted":
                    raise ValueError("会话已删除")
                max_seq = session.scalar(
                    select(func.max(Message.seq_no)).where(
                        Message.conversation_id == conversation_id
                    )
                )
                next_seq = int(max_seq or 0) + 1
                conversation.status = "active"
                conversation.updated_at = timestamp
                conversation.last_message_at = timestamp

            session.add(
                Message(
                    id=user_message_id,
                    conversation_id=conversation_id,
                    seq_no=next_seq,
                    role="user",
                    content=user_content,
                    status="completed",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            session.add(
                Message(
                    id=assistant_message_id,
                    conversation_id=conversation_id,
                    seq_no=next_seq + 1,
                    role="assistant",
                    content="",
                    status="pending",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            return conversation.agent_contract_json

    def complete_turn(
        self,
        *,
        application_id: str,
        conversation_id: str,
        assistant_message_id: str,
        answer: str,
        route: str,
        snippets: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]] | None = None,
        trace: list[dict[str, Any]] | None = None,
        usage: dict[str, int] | None = None,
        ttft_ms: int | None = None,
        images: list[dict[str, Any]] | None = None,
        image_policy: AnswerImagePolicy = AnswerImagePolicy(),
        answer_format_version: int = 1,
        answer_output_mode: str = "text",
        image_previews_unavailable: int = 0,
        now: datetime,
        session: Session | None = None,
    ) -> None:
        timestamp = now.isoformat()
        with (
            nullcontext(session)
            if session is not None
            else self.factory.session_scope(write=True)
        ) as session:
            message = self._require_assistant_message(
                session,
                application_id,
                conversation_id,
                assistant_message_id,
            )
            if message.status == "completed":
                return
            if message.status != "pending":
                raise ValueError(
                    f"assistant message 当前状态不可完成: {message.status}"
                )

            normalized = {
                "answer": answer,
                "images": images or [],
                "answer_output_mode": answer_output_mode,
            }
            normalize_completion_images(session, normalized, policy=image_policy)
            answer = normalized["answer"]
            answer_output_mode = normalized["answer_output_mode"]
            message.content = answer
            message.status = "completed"
            message.route = route
            message.error_message = None
            message.tool_calls_json = json.dumps(
                tool_calls or [], ensure_ascii=False, default=str
            )
            message.trace_json = json.dumps(
                trace or [], ensure_ascii=False, default=str
            )
            message.usage_json = (
                json.dumps(usage, ensure_ascii=False) if usage else None
            )
            message.ttft_ms = ttft_ms
            message.answer_format_version = answer_format_version
            message.answer_output_mode = answer_output_mode
            message.image_previews_unavailable = image_previews_unavailable
            message.updated_at = timestamp
            for source in list(message.sources):
                session.delete(source)
            for order, source in enumerate(snippets):
                self._insert_source(session, message, order, source, timestamp)
            session.query(MessageImage).filter(
                MessageImage.message_id == message.id
            ).delete(synchronize_session=False)
            for sequence, image in enumerate(images or []):
                bindings = valid_source_bindings(
                    session, image.get("source_bindings", [])
                )
                image_status = str(image.get("status") or "unavailable")
                reason = image.get("unavailable_reason")
                if image_status == "available" and not bindings:
                    image_status, reason = "unavailable", "source_deleted"
                if image_status == "available":
                    commit_image_blob(session, image)
                session.add(
                    MessageImage(
                        id=str(image.get("id") or uuid.uuid4()),
                        message_id=message.id,
                        blob_id=image.get("blob_id")
                        if image_status == "available"
                        else None,
                        image_id=str(image.get("image_id") or ""),
                        sequence=sequence,
                        ref_ids_json=json.dumps(
                            image.get("ref_ids", []), ensure_ascii=False
                        ),
                        occurrences_json=json.dumps(
                            image.get("occurrences", []), ensure_ascii=False
                        ),
                        source_bindings_json=json.dumps(bindings, ensure_ascii=False),
                        caption=str(image.get("caption") or ""),
                        display_in_answer=bool(image.get("display_in_answer", True)),
                        status=image_status,
                        unavailable_reason=reason,
                        media_type=image.get("media_type"),
                        size_bytes=image.get("size_bytes"),
                        width=image.get("width"),
                        height=image.get("height"),
                        orientation=int(image.get("orientation", 1)),
                        created_at=timestamp,
                    )
                )

            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.updated_at = timestamp
                conversation.last_message_at = timestamp

    def fail_turn(
        self,
        *,
        application_id: str,
        conversation_id: str,
        assistant_message_id: str,
        error_message: str,
        now: datetime,
        session: Session | None = None,
    ) -> None:
        timestamp = now.isoformat()
        with (
            nullcontext(session)
            if session is not None
            else self.factory.session_scope(write=True)
        ) as session:
            message = self._require_assistant_message(
                session,
                application_id,
                conversation_id,
                assistant_message_id,
            )
            if message.status != "pending":
                return
            message.status = "failed"
            message.error_message = error_message[:2_000]
            message.updated_at = timestamp
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.updated_at = timestamp

    @staticmethod
    def _require_assistant_message(
        session: Session,
        application_id: str,
        conversation_id: str,
        message_id: str,
    ) -> Message:
        item = session.scalar(
            select(Message)
            .join(Conversation)
            .where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Conversation.id == conversation_id,
                Conversation.application_id == application_id,
                Message.role == "assistant",
            )
        )
        if item is None:
            raise KeyError(message_id)
        return item

    @staticmethod
    def _insert_source(
        session: Session,
        message: Message,
        source_order: int,
        source: dict[str, Any],
        timestamp: str,
    ) -> None:
        text = _source_text(source)
        if not text:
            return
        doc_id = str(
            source.get("doc_id")
            or source.get("document_id")
            or source.get("source")
            or "unknown"
        )
        session.add(
            MessageSource(
                id=str(uuid.uuid4()),
                message_id=message.id,
                source_order=source_order,
                doc_id=doc_id,
                node_id=source.get("node_id"),
                line_spec=source.get("line_spec"),
                line_num=_optional_int(source.get("line_num")),
                title=str(source.get("title", "")),
                retrieved_text=text,
                char_offset=_optional_int(source.get("char_offset")),
                char_limit=_optional_int(source.get("char_limit")),
                total_chars=_optional_int(source.get("total_chars")),
                text_truncated=bool(source.get("text_truncated", False)),
                content_version=_optional_int(source.get("content_version")),
                metadata_json=json.dumps(source, ensure_ascii=False, default=str),
                created_at=timestamp,
            )
        )

    def list_conversations(
        self, application_id: str, channel: str = "console"
    ) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            conversations = session.scalars(
                select(Conversation)
                .where(
                    Conversation.application_id == application_id,
                    Conversation.channel == channel,
                    Conversation.status != "deleted",
                )
                .order_by(Conversation.updated_at.desc())
            ).all()
            result: list[dict[str, Any]] = []
            for conversation in conversations:
                message_count = session.scalar(
                    select(func.count(Message.id)).where(
                        Message.conversation_id == conversation.id
                    )
                )
                result.append(
                    {
                        "id": conversation.id,
                        "application_id": conversation.application_id,
                        "title": conversation.title,
                        "status": conversation.status,
                        "created_at": conversation.created_at,
                        "updated_at": conversation.updated_at,
                        "last_message_at": conversation.last_message_at,
                        "message_count": int(message_count or 0),
                        "channel": conversation.channel,
                        "external_user_id": conversation.external_user_id,
                    }
                )
            return result

    def get_messages(
        self, application_id: str, conversation_id: str
    ) -> list[dict[str, Any]] | None:
        with self.factory.session_scope() as session:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.application_id == application_id,
                    Conversation.status != "deleted",
                )
            )
            if conversation is None:
                return None
            messages = session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq_no)
            ).all()
            result = [_message_dict(message) for message in messages]
            if messages:
                source_items = session.scalars(
                    select(MessageSource)
                    .where(
                        MessageSource.message_id.in_(
                            [message.id for message in messages]
                        )
                    )
                    .order_by(MessageSource.message_id, MessageSource.source_order)
                ).all()
                sources_by_message: dict[str, list[dict[str, Any]]] = {}
                for source in source_items:
                    sources_by_message.setdefault(source.message_id, []).append(
                        _source_dict(source)
                    )
                for message in result:
                    message["sources"] = sources_by_message.get(message["id"], [])
                image_items = session.scalars(
                    select(MessageImage)
                    .where(
                        MessageImage.message_id.in_(
                            [message.id for message in messages]
                        )
                    )
                    .order_by(MessageImage.message_id, MessageImage.sequence)
                ).all()
                images_by_message: dict[str, list[dict[str, Any]]] = {}
                for image in image_items:
                    payload = {
                        "id": image.id,
                        "image_id": image.image_id,
                        "caption": image.caption,
                        "display_in_answer": bool(image.display_in_answer),
                        "status": image.status,
                        "unavailable_reason": image.unavailable_reason,
                        "media_type": image.media_type,
                        "size_bytes": image.size_bytes,
                        "width": image.width,
                        "height": image.height,
                        "orientation": image.orientation,
                        "ref_ids": _parse_json_list(image.ref_ids_json),
                        "occurrences": _parse_json_list(image.occurrences_json),
                        "source_bindings": _parse_json_list(image.source_bindings_json),
                    }
                    images_by_message.setdefault(image.message_id, []).append(payload)
                for message in result:
                    message["images"] = images_by_message.get(message["id"], [])
            return result

    def get_source(
        self,
        application_id: str,
        conversation_id: str,
        message_id: str,
        source_id: str,
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            source = session.scalar(
                select(MessageSource)
                .join(Message)
                .join(Conversation)
                .where(
                    MessageSource.id == source_id,
                    Message.id == message_id,
                    Conversation.id == conversation_id,
                    Conversation.application_id == application_id,
                )
            )
            return _source_dict(source) if source is not None else None

    def delete_conversation(self, application_id: str, conversation_id: str) -> bool:
        with self.factory.session_scope(write=True) as session:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.application_id == application_id,
                    Conversation.channel == "console",
                )
            )
            if conversation is None:
                return False
            session.delete(conversation)
            return True


__all__ = ["SQLiteChatRepository"]
