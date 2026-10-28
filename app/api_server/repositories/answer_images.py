"""Persistence helpers for immutable image blobs and message attachments."""

from __future__ import annotations

import json
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import (
    AnswerImageBlob,
    ApplicationRun,
    Conversation,
    Document,
    ImageAccessGrant,
    KnowledgeBase,
    Message,
    MessageImage,
)
from app.api_server.database.models.answer_images import ImageBlobReadLease
from app.api_server.database.models.publishing import (
    ApplicationApiKey,
    ApplicationApiClient,
    ApplicationDeployment,
)
from app.api_server.common.errors import ApiError
from nianlun.agent.contracts import AnswerImagePolicy


def _record(item: Any) -> dict[str, Any]:
    return {
        column.name: getattr(item, column.name) for column in item.__table__.columns
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ImageBlobReservationBusy(ValueError):
    """An active blob won the identity; retry after this transaction closes."""


def commit_image_blob(session: Session, image: dict[str, Any]) -> None:
    """Fence a prepared image in the same transaction as its attachment."""
    blob = session.get(AnswerImageBlob, image.get("blob_id"))
    now = datetime.now(timezone.utc)
    if blob is None or blob.status not in {"uploading", "ready"}:
        raise ValueError("图片 blob 当前状态不可提交")
    if image.get("sha256") != blob.sha256:
        raise ValueError("图片 blob 身份不匹配")
    expected_locator = image.get("_blob_locator")
    if expected_locator is not None and expected_locator != [
        getattr(blob, field)
        for field in ("backend", "storage_profile_id", "object_key", "version_id")
    ]:
        raise ValueError("图片 blob 定位已失效")
    if blob.status == "ready":
        if not image.get("_read_token"):
            raise ValueError("图片读取租约缺失")
        lease = session.get(ImageBlobReadLease, image["_read_token"])
        if (
            lease is None
            or lease.expires_at <= now.isoformat()
            or lease.locator_json != _locator(blob)
        ):
            raise ValueError("图片读取租约已失效")
    if blob.status == "uploading":
        token = image.get("_upload_token")
        if (
            not isinstance(token, str)
            or token != blob.lease_token
            or not blob.lease_expires_at
            or datetime.fromisoformat(blob.lease_expires_at) <= now
        ):
            raise ValueError("图片上传租约已失效")
        blob.status = "ready"
        blob.lease_token = blob.lease_expires_at = None
        blob.updated_at = now.isoformat()
    if image.get("_read_token"):
        session.execute(
            delete(ImageBlobReadLease).where(
                ImageBlobReadLease.token == image["_read_token"]
            )
        )


class SourceImageBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    doc_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=256)
    content_version: int = Field(ge=1)
    citation_id: int = Field(ge=1)
    generation: int = Field(ge=1)
    image_id: str = Field(min_length=1, max_length=128)
    char_offset: int | None = Field(default=None, ge=0)
    char_limit: int | None = Field(default=None, ge=1)
    line_spec: str | None = Field(default=None, max_length=256)


def valid_source_bindings(
    session: Session, bindings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Updates preserve old sources; missing/deleted documents never grant access."""
    valid = []
    for binding in bindings:
        try:
            binding = SourceImageBinding.model_validate(binding).model_dump(
                exclude_none=True
            )
        except ValidationError:
            continue
        doc_id = binding.get("doc_id")
        kb_id = binding.get("knowledge_base_id")
        if not isinstance(doc_id, str) or not isinstance(kb_id, str):
            continue
        document = session.get(Document, doc_id)
        if (
            document is not None
            and document.knowledge_base_id == kb_id
            and document.status != "deleted"
            and document.deleted_content_version is None
            and session.get(KnowledgeBase, kb_id) is not None
        ):
            valid.append(binding)
    return valid


def _locator(blob: Any) -> str:
    return json.dumps(
        [
            blob[name] if isinstance(blob, dict) else getattr(blob, name)
            for name in (
                "id",
                "backend",
                "storage_profile_id",
                "object_key",
                "version_id",
            )
        ]
    )


def normalize_completion_images(
    session: Session,
    raw: dict[str, Any],
    *,
    policy: AnswerImagePolicy = AnswerImagePolicy(),
) -> None:
    from nianlun.agent.answer_images import normalize_answer_images

    if not raw.get("images"):
        return
    allowed = {}
    for image in raw.get("images", []):
        bindings = valid_source_bindings(session, image.get("source_bindings", []))
        image["source_bindings"] = bindings
        if image.get("status") == "available" and not bindings:
            image.update(
                status="unavailable", unavailable_reason="source_deleted", blob_id=None
            )
        if image.get("status") == "available":
            allowed[image["id"]] = (image["id"], image.get("caption", ""))
    raw["answer"], occurrences = normalize_answer_images(
        raw["answer"],
        allowed,
        max_occurrences=policy.max_occurrences,
        max_candidate_line_bytes=policy.max_candidate_line_bytes,
        max_canonical_bytes=policy.max_canonical_bytes,
    )
    for image in raw.get("images", []):
        image["occurrences"] = [
            item for item in occurrences if item["attachment_id"] == image["id"]
        ]
    if not any(
        image.get("status") == "available" and image.get("display_in_answer", True)
        for image in raw.get("images", [])
    ):
        if raw.get("answer_output_mode") == "images" and not raw["answer"].strip():
            raw["answer"] = "图片暂不可用，请重新检索。"
        raw["answer_output_mode"] = "text"


def invalidate_source_images(session: Session) -> int:
    """Remove dead bindings atomically with document/tombstone mutations."""
    session.flush()
    changed = 0
    for image in session.scalars(
        select(MessageImage).where(MessageImage.status == "available")
    ):
        bindings = valid_source_bindings(
            session, json.loads(image.source_bindings_json)
        )
        image.source_bindings_json = json.dumps(bindings, ensure_ascii=False)
        if bindings:
            continue
        image.status = "unavailable"
        image.unavailable_reason = "source_deleted"
        image.blob_id = None
        session.execute(
            update(ImageAccessGrant)
            .where(
                ImageAccessGrant.attachment_id == image.id,
                ImageAccessGrant.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
        changed += 1
    return changed


class AnswerImageRepository:
    def __init__(self, factory: SQLiteConnectionFactory) -> None:
        self.factory = factory

    def acquire_read(self, blob: dict[str, Any]) -> str | None:
        now = datetime.now(timezone.utc)
        with self.factory.session_scope(write=True) as session:
            item = session.get(AnswerImageBlob, blob["id"])
            if (
                item is None
                or item.status != "ready"
                or _locator(item) != _locator(blob)
            ):
                return None
            token = uuid.uuid4().hex
            session.add(
                ImageBlobReadLease(
                    token=token,
                    blob_id=item.id,
                    locator_json=_locator(item),
                    expires_at=(now + timedelta(minutes=5)).isoformat(),
                )
            )
            return token

    def renew_read(self, token: str) -> bool:
        now = datetime.now(timezone.utc)
        with self.factory.session_scope(write=True) as session:
            lease = session.get(ImageBlobReadLease, token)
            if lease is None or lease.expires_at <= now.isoformat():
                return False
            item = session.get(AnswerImageBlob, lease.blob_id)
            if (
                item is None
                or item.status != "ready"
                or _locator(item) != lease.locator_json
            ):
                return False
            lease.expires_at = (now + timedelta(minutes=5)).isoformat()
            return True

    def release_read(self, token: str) -> None:
        with self.factory.session_scope(write=True) as session:
            session.execute(
                delete(ImageBlobReadLease).where(ImageBlobReadLease.token == token)
            )

    def expire_results(self) -> int:
        now = _now()
        count = 0
        with self.factory.session_scope(write=True) as session:
            session.execute(
                delete(ImageBlobReadLease).where(ImageBlobReadLease.expires_at <= now)
            )
            for run in session.scalars(
                select(ApplicationRun).where(
                    ApplicationRun.result_expires_at.is_not(None),
                    ApplicationRun.result_expires_at <= now,
                    ApplicationRun.result_json.is_not(None),
                )
            ):
                run.result_json = None
                attachments = list(
                    session.scalars(
                        select(MessageImage).where(
                            MessageImage.message_id == run.message_id
                        )
                    )
                )
                for image in attachments:
                    image.status = "unavailable"
                    image.unavailable_reason = "result_expired"
                    image.blob_id = None
                    session.execute(
                        update(ImageAccessGrant)
                        .where(
                            ImageAccessGrant.attachment_id == image.id,
                            ImageAccessGrant.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                    )
                count += 1
        return count

    def collection_candidates(self) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            return [_record(item) for item in session.scalars(select(AnswerImageBlob))]

    def claim_collection(
        self, blob: dict[str, Any], *, grace_hours: float
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with self.factory.session_scope(write=True) as session:
            item = session.get(AnswerImageBlob, blob["id"])
            if item is None or _locator(item) != _locator(blob):
                return None
            if session.scalar(
                select(ImageBlobReadLease.token).where(
                    ImageBlobReadLease.blob_id == item.id,
                    ImageBlobReadLease.expires_at > now.isoformat(),
                )
            ):
                return None
            if session.scalar(
                select(MessageImage.id).where(
                    MessageImage.blob_id == item.id, MessageImage.status == "available"
                )
            ):
                return None
            if (
                item.status in {"uploading", "deleting"}
                and item.lease_expires_at
                and item.lease_expires_at > now.isoformat()
            ):
                return None
            if item.status in {"ready", "uploading"}:
                item.status = "retired"
                item.retired_at = now.isoformat()
                item.lease_token = item.lease_expires_at = None
            if item.status == "retired" and (
                not item.retired_at
                or datetime.fromisoformat(item.retired_at)
                + timedelta(hours=grace_hours)
                > now
            ):
                return None
            item.status = "deleting"
            item.lease_token = uuid.uuid4().hex
            item.lease_expires_at = (now + timedelta(minutes=5)).isoformat()
            item.updated_at = now.isoformat()
            session.flush()
            return _record(item)

    def renew_collection(self, claim: dict[str, Any]) -> bool:
        now = datetime.now(timezone.utc)
        with self.factory.session_scope(write=True) as session:
            item = session.get(AnswerImageBlob, claim["id"])
            if (
                item is None
                or item.status != "deleting"
                or _locator(item) != _locator(claim)
                or item.lease_token != claim["lease_token"]
                or not item.lease_expires_at
                or item.lease_expires_at <= now.isoformat()
            ):
                return False
            item.lease_expires_at = (now + timedelta(minutes=5)).isoformat()
            return True

    def finish_collection(self, claim: dict[str, Any]) -> bool:
        with self.factory.session_scope(write=True) as session:
            item = session.get(AnswerImageBlob, claim["id"])
            if (
                item is None
                or item.status != "deleting"
                or _locator(item) != _locator(claim)
                or item.lease_token != claim["lease_token"]
                or not item.lease_expires_at
                or item.lease_expires_at <= _now()
            ):
                return False
            item.status = "deleted"
            item.lease_token = item.lease_expires_at = None
            item.updated_at = _now()
            return True

    def ready_blob(self, knowledge_base_id: str, sha256: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalar(
                select(AnswerImageBlob)
                .where(
                    AnswerImageBlob.knowledge_base_id == knowledge_base_id,
                    AnswerImageBlob.sha256 == sha256,
                    AnswerImageBlob.status == "ready",
                )
                .order_by(AnswerImageBlob.created_at.desc())
            )
            return _record(item) if item is not None else None

    def blob(self, blob_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.get(AnswerImageBlob, blob_id)
            return _record(item) if item is not None else None

    def attachment(
        self,
        application_id: str,
        conversation_id: str,
        message_id: str,
        attachment_id: str,
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalar(
                select(MessageImage)
                .join(Message, Message.id == MessageImage.message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    MessageImage.id == attachment_id,
                    Message.id == message_id,
                    Message.status == "completed",
                    Conversation.id == conversation_id,
                    Conversation.application_id == application_id,
                    Conversation.status != "deleted",
                )
            )
            if item is None:
                return None
            value = self._image_record(item)
            value["source_bindings"] = valid_source_bindings(
                session, value["source_bindings"]
            )
            if item.status == "available" and not value["source_bindings"]:
                value.update(status="unavailable", unavailable_reason="source_deleted")
            run = session.scalar(
                select(ApplicationRun).where(ApplicationRun.message_id == message_id)
            )
            if run is not None and (
                run.status != "completed"
                or not run.result_expires_at
                or datetime.fromisoformat(run.result_expires_at)
                <= datetime.now(timezone.utc)
            ):
                value.update(status="unavailable", unavailable_reason="result_expired")
            return value

    @staticmethod
    def _image_record(item: MessageImage) -> dict[str, Any]:
        value = _record(item)
        value["ref_ids"] = json.loads(item.ref_ids_json)
        value["occurrences"] = json.loads(item.occurrences_json)
        value["source_bindings"] = json.loads(item.source_bindings_json)
        return value

    def create_uploading(
        self,
        *,
        knowledge_base_id: str,
        sha256: str,
        media_type: str,
        size_bytes: int,
        width: int,
        height: int,
        orientation: int,
        backend: str,
        storage_profile_id: str,
        object_key: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now or _now()
        deadline = (
            datetime.fromisoformat(timestamp) + timedelta(minutes=5)
        ).isoformat()
        item = AnswerImageBlob(
            id=str(uuid.uuid4()),
            knowledge_base_id=knowledge_base_id,
            sha256=sha256,
            media_type=media_type,
            size_bytes=size_bytes,
            width=width,
            height=height,
            orientation=orientation,
            backend=backend,
            storage_profile_id=storage_profile_id,
            object_key=object_key,
            status="uploading",
            lease_token=uuid.uuid4().hex,
            lease_expires_at=deadline,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self.factory.session_scope(write=True) as session:
            existing = session.scalar(
                select(AnswerImageBlob).where(
                    AnswerImageBlob.knowledge_base_id == knowledge_base_id,
                    AnswerImageBlob.sha256 == sha256,
                    AnswerImageBlob.status.in_(["uploading", "ready"]),
                )
            )
            if existing is not None:
                if existing.status == "ready" or (
                    existing.lease_expires_at
                    and datetime.fromisoformat(existing.lease_expires_at)
                    > datetime.fromisoformat(timestamp)
                ):
                    raise ImageBlobReservationBusy("图片上传正忙，请稍后重试")
                existing.status = "retired"
                existing.retired_at = existing.updated_at = timestamp
                existing.lease_token = existing.lease_expires_at = None
                session.flush()
            session.add(item)
            session.flush()
            return _record(item)

    def stage_upload(
        self,
        blob_id: str,
        *,
        token: str,
        version_id: str | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            item = session.get(AnswerImageBlob, blob_id)
            if item is None:
                raise KeyError(blob_id)
            timestamp = now or _now()
            if (
                item.status != "uploading"
                or item.lease_token != token
                or not item.lease_expires_at
                or datetime.fromisoformat(item.lease_expires_at)
                <= datetime.fromisoformat(timestamp)
            ):
                raise ValueError("图片上传租约或状态已失效")
            item.version_id = version_id
            item.updated_at = timestamp
            session.flush()
            return _record(item)

    def renew_upload(self, blob_id: str, token: str) -> bool:
        now = datetime.now(timezone.utc)
        with self.factory.session_scope(write=True) as session:
            result = session.execute(
                update(AnswerImageBlob)
                .where(
                    AnswerImageBlob.id == blob_id,
                    AnswerImageBlob.status == "uploading",
                    AnswerImageBlob.lease_token == token,
                    AnswerImageBlob.lease_expires_at > now.isoformat(),
                )
                .values(
                    lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            return getattr(result, "rowcount", 0) == 1

    def retire_blob(
        self, blob_id: str, *, token: str | None = None, now: str | None = None
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.get(AnswerImageBlob, blob_id)
            if item is None or item.status not in {"uploading", "ready"}:
                return
            if token is not None and (
                item.status != "uploading" or item.lease_token != token
            ):
                return
            timestamp = now or _now()
            item.status = "retired"
            item.retired_at = timestamp
            item.updated_at = timestamp
            item.lease_token = item.lease_expires_at = None
            ids = select(MessageImage.id).where(MessageImage.blob_id == blob_id)
            session.execute(
                update(ImageAccessGrant)
                .where(
                    ImageAccessGrant.attachment_id.in_(ids),
                    ImageAccessGrant.revoked_at.is_(None),
                )
                .values(revoked_at=timestamp)
            )
            session.execute(
                update(MessageImage)
                .where(MessageImage.blob_id == blob_id)
                .values(
                    status="unavailable",
                    unavailable_reason="blob_retired",
                    blob_id=None,
                )
            )

    def add_message_images(
        self,
        *,
        message_id: str,
        images: list[dict[str, Any]],
        session: Any,
        now: str | None = None,
    ) -> None:
        timestamp = now or _now()
        for sequence, values in enumerate(images):
            session.add(
                MessageImage(
                    id=str(values.get("id") or uuid.uuid4()),
                    message_id=message_id,
                    blob_id=values.get("blob_id"),
                    image_id=str(values["image_id"]),
                    sequence=sequence,
                    ref_ids_json=json.dumps(
                        values.get("ref_ids", []), ensure_ascii=False
                    ),
                    occurrences_json=json.dumps(
                        values.get("occurrences", []), ensure_ascii=False
                    ),
                    source_bindings_json=json.dumps(
                        values.get("source_bindings", []), ensure_ascii=False
                    ),
                    caption=str(values.get("caption") or ""),
                    display_in_answer=bool(values.get("display_in_answer", True)),
                    status=str(values.get("status") or "unavailable"),
                    unavailable_reason=values.get("unavailable_reason"),
                    media_type=values.get("media_type"),
                    size_bytes=values.get("size_bytes"),
                    width=values.get("width"),
                    height=values.get("height"),
                    orientation=int(values.get("orientation", 1)),
                    created_at=timestamp,
                )
            )

    def message_images(self, message_id: str) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            items = session.scalars(
                select(MessageImage)
                .where(MessageImage.message_id == message_id)
                .order_by(MessageImage.sequence)
            ).all()
            result: list[dict[str, Any]] = []
            for item in items:
                result.append(self._image_record(item))
            return result

    def grant(self, grant_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.get(ImageAccessGrant, grant_id)
            return _record(item) if item is not None else None

    @staticmethod
    def _grant_authorized(session: Session, grant: ImageAccessGrant) -> None:
        now = datetime.now(timezone.utc)
        credential = session.get(ApplicationApiKey, grant.key_id)
        client = session.get(ApplicationApiClient, grant.client_id)
        deployment = session.get(ApplicationDeployment, grant.application_id)
        if (
            credential is None
            or credential.client_id != grant.client_id
            or credential.revoked_at
            or (
                credential.expires_at
                and datetime.fromisoformat(credential.expires_at) <= now
            )
            or client is None
            or client.application_id != grant.application_id
            or not client.enabled
            or deployment is None
            or not deployment.enabled
        ):
            raise ApiError("图片不存在", 404)

    def grant_context(self, grant_id: str, token: str) -> dict[str, Any]:
        with self.factory.session_scope() as session:
            grant = session.get(ImageAccessGrant, grant_id)
            if (
                grant is None
                or grant.revoked_at
                or not hmac.compare_digest(
                    grant.token_sha256, hashlib.sha256(token.encode()).hexdigest()
                )
            ):
                raise ApiError("图片不存在", 404)
            self._grant_authorized(session, grant)
            run = session.scalar(
                select(ApplicationRun)
                .join(
                    MessageImage, ApplicationRun.message_id == MessageImage.message_id
                )
                .where(MessageImage.id == grant.attachment_id)
            )
            if (
                run is None
                or run.status != "completed"
                or not run.result_expires_at
                or run.application_id != grant.application_id
                or run.client_id != grant.client_id
            ):
                raise ApiError("图片不存在", 404)
            now = datetime.now(timezone.utc)
            if (
                datetime.fromisoformat(grant.expires_at) <= now
                or datetime.fromisoformat(run.result_expires_at) <= now
            ):
                raise ApiError("图片授权已过期", 410)
            return {"grant": _record(grant), "run": _record(run)}

    def save_grant(self, values: dict[str, Any], run_id: str, user_id: str) -> None:
        with self.factory.session_scope(write=True) as session:
            grant = ImageAccessGrant(**values)
            self._grant_authorized(session, grant)
            run = session.get(ApplicationRun, run_id)
            image = session.get(MessageImage, grant.attachment_id)
            if (
                run is None
                or image is None
                or image.message_id != run.message_id
                or run.application_id != grant.application_id
                or run.client_id != grant.client_id
                or run.user_id != user_id
                or run.status != "completed"
                or image.status != "available"
                or not image.display_in_answer
                or not valid_source_bindings(
                    session, json.loads(image.source_bindings_json)
                )
            ):
                raise ApiError("图片不存在", 404)
            blob = (
                session.get(AnswerImageBlob, image.blob_id) if image.blob_id else None
            )
            if blob is None or blob.status != "ready":
                raise ApiError("图片不存在", 404)
            conversation = session.get(Conversation, run.conversation_id)
            if conversation is None or conversation.status == "deleted":
                raise ApiError("图片不存在", 404)
            if not run.result_expires_at:
                raise ApiError("历史运行不支持图片授权", 409)
            if datetime.fromisoformat(run.result_expires_at) <= datetime.now(
                timezone.utc
            ):
                raise ApiError("调用结果已过期", 410)
            if run.image_transport != "url":
                raise ApiError("内联图片结果不支持刷新链接", 409)
            session.add(grant)


__all__ = ["AnswerImageRepository"]
