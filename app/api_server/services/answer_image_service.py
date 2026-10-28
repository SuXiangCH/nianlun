"""Select, validate and archive logical answer-image references."""

from __future__ import annotations

import uuid
import logging
import threading
import time
from sqlalchemy.exc import SQLAlchemyError
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from collections.abc import Generator
from app.api_server.services.image_blob_lifecycle import media_lock, RenewingLease
from pathlib import Path
from typing import Any, Callable, Literal, cast
from urllib.parse import quote

from app.api_server.common.errors import ApiError
from app.api_server.repositories.answer_images import (
    AnswerImageRepository,
    ImageBlobReservationBusy,
)
from app.api_server.storage import ImageBlobLocation, ImageMetadata, validate_image
from app.api_server.storage.image_validation import ImageValidationError
from app.api_server.storage.image_blob_store import BlobStoreError, ImageBlobStore
from app.api_server.storage.filesystem_image_blob_store import FilesystemImageBlobStore
from nianlun.agent.answer_images import (
    add_cited_source_images,
    answer_image_references,
    normalize_answer_images,
)
from nianlun.agent.contracts import AnswerImagePolicy
from nianlun.agent.language import prefers_english

logger = logging.getLogger(__name__)

_UPLOAD_WAIT_SECONDS = 5.0
_UPLOAD_POLL_SECONDS = 0.05


class AnswerImageService:
    """Keep model output logical; only this service resolves bytes and storage."""

    def __init__(
        self,
        repository: AnswerImageRepository,
        blob_store: ImageBlobStore,
        knowledge_base_lookup: Callable[[str], dict[str, Any]],
        application_lookup: Callable[[str], dict[str, Any]],
        *,
        profile_id: str,
        backend: str = "filesystem",
    ) -> None:
        self.repository = repository
        self.blob_store = blob_store
        self.knowledge_base_lookup = knowledge_base_lookup
        self.application_lookup = application_lookup
        self.profile_id = profile_id
        self.backend = backend
        self._scope: ContextVar[ExitStack | None] = ContextVar(
            "image_completion", default=None
        )
        self._lock_root = repository.factory.path.parent / ".image-media-locks"
        self._gc_stop = threading.Event()
        self._gc_worker: threading.Thread | None = None

    def start_gc(self, *, grace_hours: int = 24) -> None:
        def run() -> None:
            while not self._gc_stop.is_set():
                try:
                    self.repository.expire_results()
                    self.collect_orphans(grace_hours=grace_hours)
                except (OSError, SQLAlchemyError):
                    logger.exception("answer_image.gc_failed")
                if self._gc_stop.wait(60):
                    return

        self._gc_worker = threading.Thread(target=run, name="image-gc", daemon=True)
        self._gc_worker.start()

    @contextmanager
    def completion_scope(self) -> Generator[None, None, None]:
        with ExitStack() as stack:
            token = self._scope.set(stack)
            try:
                yield
            finally:
                self._scope.reset(token)

    def shutdown(self) -> None:
        """Release SDK clients after the request and background workers stop."""
        self._gc_stop.set()
        if self._gc_worker is not None:
            self._gc_worker.join()
        close = getattr(self.blob_store, "close", None)
        if close is not None:
            close()

    def prepare_result(
        self,
        application_id: str,
        result: dict[str, Any],
        *,
        knowledge_base_id: str | None = None,
        policy: AnswerImagePolicy = AnswerImagePolicy(),
        question: str = "",
    ) -> dict[str, Any]:
        """Resolve validated markers in one Agent result into message attachments."""
        answer = str(result.get("answer") or "")
        pure_images = result.get("answer_output_mode") == "images"
        snippets = result.get("retrieved_snippets")
        if not isinstance(snippets, list):
            snippets = []
        candidates: dict[str, dict[str, Any]] = {}
        application = self.application_lookup(application_id)
        knowledge_base_id = knowledge_base_id or str(application["knowledge_base_id"])
        for snippet in snippets:
            if not isinstance(snippet, dict):
                continue
            citation_id = snippet.get("citation_id")
            images = snippet.get("images")
            if not isinstance(images, list):
                continue
            for image in images:
                if not isinstance(image, dict) or not isinstance(
                    image.get("image_id"), str
                ):
                    continue
                candidate = dict(image)
                ref_id = image.get("ref_id", image["image_id"])
                if (
                    not isinstance(ref_id, str)
                    or image.get("knowledge_base_id", knowledge_base_id)
                    != knowledge_base_id
                ):
                    continue
                binding = {
                    "knowledge_base_id": knowledge_base_id,
                    "doc_id": snippet.get("doc_id"),
                    "node_id": snippet.get("node_id"),
                    "content_version": image.get(
                        "content_version", snippet.get("content_version")
                    ),
                    "citation_id": citation_id,
                    "generation": image.get("generation", snippet.get("generation")),
                    "image_id": image["image_id"],
                }
                candidate["source_bindings"] = [binding]
                existing = candidates.setdefault(ref_id, candidate)
                if binding not in existing["source_bindings"]:
                    existing["source_bindings"].append(binding)

        scope = self._scope.get()
        if scope is not None and self.backend == "filesystem":
            scope.enter_context(
                media_lock(
                    self._lock_root,
                    [
                        (
                            knowledge_base_id,
                            str(candidate.get("sha256") or candidate["image_id"]),
                        )
                        for candidate in candidates.values()
                    ],
                )
            )
        attachments: list[dict[str, Any]] = []
        allowed: dict[str, tuple[str, str]] = {}
        limits = {
            "max_occurrences": policy.max_occurrences,
            "max_candidate_line_bytes": policy.max_candidate_line_bytes,
            "max_canonical_bytes": policy.max_canonical_bytes,
        }
        references = answer_image_references(answer, **limits)
        input_citations = result.get("answer_input_citation_ids")

        def resource_key(candidate: dict[str, Any]) -> tuple[str, str]:
            return knowledge_base_id, str(
                candidate.get("sha256") or candidate["image_id"]
            )

        if pure_images:
            selected = result.get("selected_answer_images") or []
            references = list(
                dict.fromkeys(
                    image["ref_id"]
                    for image in selected
                    if isinstance(image, dict) and isinstance(image.get("ref_id"), str)
                )
            )
            if len(references) > policy.max_occurrences:
                raise ValueError("答案图片引用次数超出限制")
        else:
            explicit_resources = {
                resource_key(candidates[ref])
                for ref in references
                if ref in candidates
                and (
                    not isinstance(input_citations, list)
                    or any(
                        binding.get("citation_id") in input_citations
                        for binding in candidates[ref]["source_bindings"]
                    )
                )
            }
            canonical_refs: dict[tuple[str, str], str] = {}
            images_by_citation: dict[int, list[tuple[str, str]]] = {}
            for ref, candidate in candidates.items():
                key = resource_key(candidate)
                if key in explicit_resources:
                    continue
                for binding in candidate["source_bindings"]:
                    citation = binding.get("citation_id")
                    if (
                        not isinstance(citation, int)
                        or isinstance(citation, bool)
                        or citation <= 0
                        or (
                            isinstance(input_citations, list)
                            and citation not in input_citations
                        )
                    ):
                        continue
                    canonical_ref = canonical_refs.setdefault(key, ref)
                    images_by_citation.setdefault(citation, []).append(
                        (canonical_ref, str(candidate.get("alt_text") or "原文图片"))
                    )
            source_contexts: dict[tuple[int, str], str] = {}
            source_positions: dict[tuple[int, str], int] = {}
            for snippet in snippets:
                if not isinstance(snippet, dict):
                    continue
                citation = snippet.get("citation_id")
                if not isinstance(citation, int) or citation not in images_by_citation:
                    continue
                text = str(snippet.get("text") or "")
                offset = int(snippet.get("char_offset") or 0)
                cursor = 0
                preceding_context = ""
                images = snippet.get("images") or []
                positioned = [
                    image
                    for image in images
                    if isinstance(image, dict)
                    and isinstance(image.get("source_start"), int)
                    and isinstance(image.get("source_end"), int)
                ]
                for image in sorted(positioned, key=lambda item: item["source_start"]):
                    start = image["source_start"] - offset
                    end = image["source_end"] - offset
                    if not 0 <= cursor <= start < end <= len(text):
                        continue
                    context = text[max(cursor, start - 1600) : start]
                    if context.strip():
                        preceding_context = context
                    ref = canonical_refs.get(resource_key(image))
                    if ref is not None:
                        source_contexts.setdefault((citation, ref), preceding_context)
                        source_positions.setdefault((citation, ref), start)
                    cursor = end
            for image_citation, images in images_by_citation.items():
                images.sort(
                    key=lambda item: source_positions.get(
                        (image_citation, item[0]), float("inf")
                    )
                )
            answer = add_cited_source_images(
                answer, images_by_citation, source_contexts=source_contexts, **limits
            )
            references = answer_image_references(answer, **limits)
        answer_bytes = 0
        answer_resources = 0
        resources: dict[tuple[str, str], dict[str, Any]] = {}

        def merge_resource(
            attachment: dict[str, Any], candidate: dict[str, Any], ref_id: str
        ) -> None:
            if ref_id not in attachment["ref_ids"]:
                attachment["ref_ids"].append(ref_id)
            for binding in candidate["source_bindings"]:
                if binding not in attachment["source_bindings"]:
                    attachment["source_bindings"].append(binding)

        for ref_id in references:
            candidate = candidates.get(ref_id)
            if candidate is None:
                continue
            if (
                not pure_images
                and isinstance(input_citations, list)
                and not any(
                    binding.get("citation_id") in input_citations
                    for binding in candidate["source_bindings"]
                )
            ):
                continue
            key = resource_key(candidate)
            if key in resources:
                attachment = resources[key]
                merge_resource(attachment, candidate, ref_id)
                if attachment["status"] == "available":
                    allowed[ref_id] = (attachment["id"], attachment["caption"])
                continue
            size = int(candidate.get("size_bytes") or 0)
            reason = policy.archive_budget_reason(
                size,
                int(candidate.get("width") or 0) * int(candidate.get("height") or 0),
                answer_resources,
                answer_bytes,
            )
            if reason:
                attachment = self._unavailable_candidate(candidate, ref_id)
                attachment["unavailable_reason"] = reason
                attachments.append(attachment)
                resources[key] = attachment
                continue
            attachment = self._archive_candidate(candidate, ref_id, policy=policy)
            attachments.append(attachment)
            resources[key] = attachment
            if attachment["status"] == "available":
                answer_resources += 1
                answer_bytes += size
                allowed[ref_id] = (attachment["id"], attachment["caption"])
        normalized, occurrences = (
            ("", [])
            if pure_images
            else normalize_answer_images(
                answer,
                allowed,
                **limits,
                unavailable_text="Image unavailable"
                if prefers_english(question)
                else "图片暂不可用",
            )
        )
        for attachment in attachments:
            attachment["occurrences"] = [
                item
                for item in occurrences
                if item["attachment_id"] == attachment["id"]
            ]
        # Source previews have a separate budget and never consume answer capacity.
        preview_bytes = 0
        previews = 0
        previews_unavailable = 0
        rejected_previews: set[tuple[str, str]] = set()
        for ref_id, candidate in candidates.items():
            if ref_id in references:
                continue
            key = resource_key(candidate)
            if key in resources:
                merge_resource(resources[key], candidate, ref_id)
                continue
            if key in rejected_previews:
                continue
            size = int(candidate.get("size_bytes") or 0)
            if (
                policy.inline_budget_enabled
                or previews >= policy.max_preview_images
                or preview_bytes + size > policy.max_preview_bytes
            ):
                previews_unavailable += 1
                rejected_previews.add(key)
                continue
            attachment = self._archive_candidate(candidate, ref_id, policy=policy)
            if attachment["status"] == "available":
                attachment["display_in_answer"] = False
                attachments.append(attachment)
                resources[key] = attachment
                previews += 1
                preview_bytes += size
            else:
                previews_unavailable += 1
                rejected_previews.add(key)
        mode = (
            "images"
            if pure_images and allowed
            else "text_and_images"
            if allowed
            else "text"
        )
        if pure_images and not allowed:
            normalized = (
                "Images are unavailable. Please search again."
                if prefers_english(question)
                else "图片暂不可用，请重新检索。"
            )
        return {
            **result,
            "answer": normalized,
            "images": attachments,
            "answer_format_version": 2,
            "answer_output_mode": mode,
            "image_previews_unavailable": previews_unavailable,
            "display_image_citation_ids": list(
                dict.fromkeys(
                    binding["citation_id"]
                    for attachment in attachments
                    if attachment["display_in_answer"]
                    and attachment["status"] == "available"
                    for binding in attachment["source_bindings"]
                    if isinstance(binding.get("citation_id"), int)
                )
            ),
        }

    @staticmethod
    def _unavailable_candidate(
        candidate: dict[str, Any], image_id: str
    ) -> dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "image_id": candidate["image_id"],
            "caption": str(candidate.get("alt_text") or "原文图片"),
            "ref_ids": [image_id],
            "occurrences": [],
            "source_bindings": candidate.get("source_bindings", []),
            "display_in_answer": True,
            "status": "unavailable",
            "unavailable_reason": "image_unavailable",
            "orientation": int(candidate.get("orientation") or 1),
        }

    def _archive_candidate(
        self,
        candidate: dict[str, Any],
        image_id: str,
        *,
        policy: AnswerImagePolicy = AnswerImagePolicy(),
    ) -> dict[str, Any]:
        image = self._unavailable_candidate(candidate, image_id)
        try:
            bindings = candidate.get("source_bindings") or []
            source_binding = bindings[0] if bindings else {}
            kb_id = str(
                source_binding.get("knowledge_base_id")
                or candidate.get("knowledge_base_id")
                or ""
            )
            if not kb_id:
                raise BlobStoreError("invalid_location")
            kb = self.knowledge_base_lookup(kb_id)
            workspace = Path(str(kb["workspace_dir"]))
            relpath = str(candidate["asset_relpath"])
            expected = ImageMetadata.model_validate(
                {field: candidate[field] for field in ImageMetadata.model_fields}
            )
            if expected.size_bytes > policy.max_image_bytes:
                raise ImageValidationError("image_too_large")
            if expected.width * expected.height > policy.max_image_pixels:
                raise ImageValidationError("pixel_limit_exceeded")
            source_store = FilesystemImageBlobStore(
                workspace,
                profile_id="workspace",
                max_bytes=policy.max_image_bytes,
                max_pixels=policy.max_image_pixels,
            )
            with source_store.open(
                ImageBlobLocation(
                    backend="filesystem",
                    storage_profile_id="workspace",
                    object_key=relpath,
                ),
                expected,
            ) as source:
                raw = source.read()
            metadata = validate_image(
                raw,
                max_bytes=policy.max_image_bytes,
                max_pixels=policy.max_image_pixels,
            )
            if candidate.get("sha256") != metadata.sha256:
                raise BlobStoreError("integrity_error")
            blob = self._archive_blob(kb_id, metadata, raw)
            image.update(
                {
                    "blob_id": blob["id"],
                    "_upload_token": blob.get("lease_token"),
                    "_read_token": blob.get("_read_token"),
                    "_blob_locator": [
                        blob.get(field)
                        for field in (
                            "backend",
                            "storage_profile_id",
                            "object_key",
                            "version_id",
                        )
                    ],
                    "sha256": metadata.sha256,
                    "status": "available",
                    "unavailable_reason": None,
                    "media_type": metadata.media_type,
                    "size_bytes": metadata.size_bytes,
                    "width": metadata.width,
                    "height": metadata.height,
                    "orientation": metadata.orientation,
                }
            )
        except (OSError, ValueError, KeyError, BlobStoreError) as exc:
            reason = (
                exc.reason
                if isinstance(exc, (BlobStoreError, ImageValidationError))
                else "image_unavailable"
            )
            image["unavailable_reason"] = reason
            logger.warning(
                "answer_image.archive_unavailable image_id=%s reason=%s error_class=%s",
                image_id,
                reason,
                type(exc).__name__,
            )
        return image

    def collect_orphans(self, *, grace_hours: float = 24) -> int:
        """Claim exact immutable locators, then perform physical IO outside SQLite."""
        collected = 0
        for candidate in self.repository.collection_candidates():
            with media_lock(
                self._lock_root, [(candidate["knowledge_base_id"], candidate["sha256"])]
            ):
                collected += self._collect_candidate(candidate, grace_hours)
        return collected

    def _collect_candidate(self, candidate: dict[str, Any], grace_hours: float) -> int:
        claim = self.repository.claim_collection(candidate, grace_hours=grace_hours)
        if claim is None:
            return 0
        location = ImageBlobLocation.model_validate(
            {field: claim[field] for field in ImageBlobLocation.model_fields}
        )
        renewal = RenewingLease(lambda: self.repository.renew_collection(claim))
        try:
            self.blob_store.delete(location, all_versions=True)
            if not renewal.valid:
                return 0
            return int(self.repository.finish_collection(claim))
        except BlobStoreError as exc:
            logger.warning(
                "answer_image.gc_retry blob_id=%s reason=%s", claim["id"], exc.reason
            )
            return 0
        finally:
            renewal.close()

    def console_images(
        self, application_id: str, conversation_id: str, message_id: str
    ) -> list[dict[str, Any]]:
        images = []
        for stored in self.repository.message_images(message_id):
            image = self.repository.attachment(
                application_id, conversation_id, message_id, stored["id"]
            )
            if image is None:
                continue
            payload = {
                field: image.get(field)
                for field in (
                    "id",
                    "ref_ids",
                    "caption",
                    "display_in_answer",
                    "status",
                    "unavailable_reason",
                    "media_type",
                    "size_bytes",
                    "width",
                    "height",
                    "orientation",
                )
            }
            payload["citation_ids"] = list(
                dict.fromkeys(
                    binding["citation_id"]
                    for binding in image["source_bindings"]
                    if isinstance(binding.get("citation_id"), int)
                )
            )
            payload["url"] = None
            if image["status"] == "available":
                parts = [
                    quote(part, safe="")
                    for part in (
                        application_id,
                        conversation_id,
                        message_id,
                        image["id"],
                    )
                ]
                payload["url"] = (
                    f"/api/v1/apps/{parts[0]}/conversations/{parts[1]}/messages/"
                    f"{parts[2]}/images/{parts[3]}/content"
                )
            images.append(payload)
        return images

    def read_content(
        self,
        application_id: str,
        conversation_id: str,
        message_id: str,
        attachment_id: str,
    ) -> tuple[bytes, str]:
        image = self.repository.attachment(
            application_id, conversation_id, message_id, attachment_id
        )
        if image is None or image["status"] != "available" or not image["blob_id"]:
            raise ApiError("图片不存在", 404)
        blob = self.repository.blob(image["blob_id"])
        if blob is None or blob["status"] != "ready":
            raise ApiError("图片不存在", 404)
        with ExitStack() as read_stack:
            if self._scope.get() is None:
                read_stack.enter_context(
                    media_lock(
                        self._lock_root, [(blob["knowledge_base_id"], blob["sha256"])]
                    )
                )
            token = self.repository.acquire_read(blob)
            if token is None:
                raise ApiError("图片不存在", 404)
            renewal = RenewingLease(lambda: self.repository.renew_read(token))
            try:
                raw = self._read_blob(blob)
                current = self.repository.attachment(
                    application_id, conversation_id, message_id, attachment_id
                )
                if (
                    current is None
                    or current["status"] != "available"
                    or current["blob_id"] != blob["id"]
                ):
                    raise ApiError("图片不存在", 404)
                if not renewal.valid or not self.repository.renew_read(token):
                    raise ApiError("图片不存在", 404)
                return raw, blob["media_type"]
            except BlobStoreError as exc:
                if exc.reason in {"image_missing", "integrity_error"}:
                    self.repository.retire_blob(blob["id"])
                logger.warning(
                    "answer_image.read_unavailable attachment_id=%s reason=%s",
                    attachment_id,
                    exc.reason,
                )
                raise ApiError("图片不存在", 404) from None
            finally:
                renewal.close()
                self.repository.release_read(token)

    def _read_blob(self, blob: dict[str, Any]) -> bytes:
        metadata = ImageMetadata.model_validate(
            {field: blob[field] for field in ImageMetadata.model_fields}
        )
        location = ImageBlobLocation.model_validate(
            {field: blob[field] for field in ImageBlobLocation.model_fields}
        )
        with self.blob_store.open(location, metadata) as source:
            return source.read()

    def read_prepared_image(self, image: dict[str, Any]) -> bytes | None:
        """Owners may read staging bytes; ready reads require a renewable lease."""
        blob_id = image.get("blob_id")
        if not isinstance(blob_id, str):
            return None
        blob = self.repository.blob(blob_id)
        if blob is None or blob["status"] not in {"uploading", "ready"}:
            return None
        if image.get("sha256") is not None and image["sha256"] != blob["sha256"]:
            return None
        with ExitStack() as read_stack:
            if self._scope.get() is None:
                read_stack.enter_context(
                    media_lock(
                        self._lock_root, [(blob["knowledge_base_id"], blob["sha256"])]
                    )
                )
            token = None
            if blob["status"] == "uploading":
                if not image.get("_upload_token") or not self.repository.renew_upload(
                    blob_id, image["_upload_token"]
                ):
                    return None
                renewal = RenewingLease(
                    lambda: self.repository.renew_upload(
                        blob_id, image["_upload_token"]
                    )
                )
            else:
                token = self.repository.acquire_read(blob)
                if token is None:
                    return None
                renewal = RenewingLease(lambda: self.repository.renew_read(token))
            try:
                raw = self._read_blob(blob)
                valid = (
                    self.repository.renew_read(token)
                    if token
                    else self.repository.renew_upload(blob_id, image["_upload_token"])
                )
                return raw if renewal.valid and valid else None
            except BlobStoreError as exc:
                if exc.reason in {"image_missing", "integrity_error"}:
                    self.repository.retire_blob(blob_id)
                logger.warning(
                    "answer_image.read_unavailable attachment_id=%s reason=%s",
                    image.get("id"),
                    exc.reason,
                )
                return None
            finally:
                renewal.close()
                if token:
                    self.repository.release_read(token)

    def _archive_blob(
        self, knowledge_base_id: str, metadata: ImageMetadata, data: bytes
    ) -> dict[str, Any]:
        deadline = time.monotonic() + _UPLOAD_WAIT_SECONDS
        while True:
            try:
                return self._try_archive_blob(knowledge_base_id, metadata, data)
            except ImageBlobReservationBusy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BlobStoreError("storage_unavailable") from None
                # Reservation transactions have closed before yielding to the owner.
                time.sleep(min(_UPLOAD_POLL_SECONDS, remaining))

    def _try_archive_blob(
        self, knowledge_base_id: str, metadata: ImageMetadata, data: bytes
    ) -> dict[str, Any]:
        existing = self.repository.ready_blob(knowledge_base_id, metadata.sha256)
        if existing is not None:
            location = ImageBlobLocation(
                backend=existing["backend"],
                storage_profile_id=existing["storage_profile_id"],
                object_key=existing["object_key"],
                version_id=existing.get("version_id"),
            )
            try:
                token = self.repository.acquire_read(existing)
                if token is None:
                    raise BlobStoreError("storage_unavailable")
                scope = self._scope.get()
                renewal = RenewingLease(lambda: self.repository.renew_read(token))
                if scope is not None:
                    scope.callback(self.repository.release_read, token)
                    scope.callback(renewal.close)
                try:
                    with self.blob_store.open(location, metadata) as source:
                        source.read()
                    if not self.repository.renew_read(token):
                        raise BlobStoreError("storage_unavailable")
                    existing["_read_token"] = token
                    if scope is None:
                        renewal.close()
                    return existing
                except BaseException:
                    renewal.close()
                    self.repository.release_read(token)
                    raise
            except BlobStoreError as exc:
                if exc.reason not in {"image_missing", "integrity_error"}:
                    raise
                self.repository.retire_blob(existing["id"])
        extension = metadata.media_type.rsplit("/", 1)[1].replace("jpeg", "jpg")
        object_key = f"answer-images/{knowledge_base_id}/{uuid.uuid4().hex}/{metadata.sha256}.{extension}"
        backend = self.backend
        if backend not in {"filesystem", "s3"}:
            raise BlobStoreError("invalid_location")
        location = ImageBlobLocation(
            backend=cast(Literal["filesystem", "s3"], backend),
            storage_profile_id=self.profile_id,
            object_key=object_key,
        )
        blob = self.repository.create_uploading(
            knowledge_base_id=knowledge_base_id,
            sha256=metadata.sha256,
            media_type=metadata.media_type,
            size_bytes=metadata.size_bytes,
            width=metadata.width,
            height=metadata.height,
            orientation=metadata.orientation,
            backend=self.backend,
            storage_profile_id=self.profile_id,
            object_key=object_key,
        )
        scope = self._scope.get()
        if scope is not None:
            scope.callback(
                self.repository.retire_blob, blob["id"], token=blob["lease_token"]
            )
            renewal = RenewingLease(
                lambda: self.repository.renew_upload(blob["id"], blob["lease_token"])
            )
            scope.callback(renewal.close)
        try:
            uploaded = self.blob_store.put(location, data, metadata)
            with self.blob_store.open(uploaded, metadata) as source:
                source.read()
            return self.repository.stage_upload(
                blob["id"], token=blob["lease_token"], version_id=uploaded.version_id
            )
        except BaseException:
            self.repository.retire_blob(blob["id"], token=blob["lease_token"])
            raise


__all__ = ["AnswerImageService"]
