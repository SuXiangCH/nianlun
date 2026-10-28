"""Version-bound Markdown previews over committed document image catalogs."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from pydantic import ValidationError

from app.api_server.apis.v1.schemas import (
    DocumentPreviewImageResponse,
    DocumentPreviewResponse,
)
from app.api_server.common.errors import ApiError
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.documents.image_catalog import (
    ImageCatalog,
    ImageCatalogEntry,
)
from app.api_server.services.knowledge_base_service import KnowledgeBaseService
from app.api_server.storage.filesystem_image_blob_store import FilesystemImageBlobStore
from app.api_server.storage.image_blob_store import BlobStoreError, ImageBlobLocation
from app.api_server.storage.image_validation import ImageMetadata
from nianlun.agent.answer_images import escape_image_caption
from nianlun.knowledgebase.core import sanitize_text
from nianlun.knowledgebase.image_syntax import parse_image_syntax
from nianlun.knowledgebase.workspace_view import WorkspaceView, WorkspaceViewError

logger = logging.getLogger(__name__)


class DocumentPreviewService:
    def __init__(
        self,
        repository: SQLiteMetadataRepository,
        knowledge_bases: KnowledgeBaseService,
    ) -> None:
        self.repository = repository
        self.knowledge_bases = knowledge_bases

    @contextmanager
    def _view(
        self, knowledge_base_id: str, document_id: str, version: int | None = None
    ) -> Generator[tuple[WorkspaceView, int]]:
        record = self.knowledge_bases.require_record(knowledge_base_id)
        if self.repository.get_document(knowledge_base_id, document_id) is None:
            raise ApiError("文档不存在", 404)
        version = int(record["content_version"]) if version is None else version
        revision = self.repository.get_revision(knowledge_base_id, version)
        if revision is None:
            raise ApiError("文档版本不存在或已过期", 404)
        view = WorkspaceView.revision(
            Path(record["workspace_dir"]),
            revision["snapshot_relpath"],
            revision["manifest_sha256"],
            knowledge_base_id=knowledge_base_id,
            content_version=version,
        )
        try:
            if document_id not in view.meta:
                raise ApiError("文档不存在", 404)
            yield view, version
        finally:
            view.close()

    @staticmethod
    def _catalog(view: WorkspaceView, document_id: str) -> ImageCatalog | None:
        # Preview metadata must remain readable when one image file is missing.
        payload = view.read_image_catalog(document_id, verify_assets=False)
        return ImageCatalog.model_validate(payload) if payload is not None else None

    @staticmethod
    def _catalog_sources(
        view: WorkspaceView, document_id: str, content: str, catalog: ImageCatalog
    ) -> dict[str, ImageCatalogEntry]:
        by_source = {item.source: item for item in catalog.entries}
        # Catalogs deduplicate identical bytes across filenames. Their trusted
        # source ranges still bind each alias to the same immutable image.
        bindings = {
            (occurrence.node_id, occurrence.char_offset, occurrence.char_end): entry
            for entry in catalog.entries
            for occurrence in entry.occurrences
            if occurrence.char_offset is not None and occurrence.char_end is not None
        }
        nodes = list(view.load_document(document_id).get("structure", []))
        while nodes:
            node = nodes.pop()
            if not isinstance(node, dict):
                continue
            nodes.extend(node.get("nodes") or [])
            for syntax in parse_image_syntax(
                sanitize_text(str(node.get("text") or "")), reference_markdown=content
            ).images:
                entry = bindings.get(
                    (str(node.get("node_id")), syntax.start, syntax.end)
                )
                if entry is not None:
                    by_source.setdefault(syntax.source, entry)
        return by_source

    def preview(
        self, knowledge_base_id: str, document_id: str
    ) -> DocumentPreviewResponse:
        try:
            with self._view(knowledge_base_id, document_id) as (view, version):
                content = view.read_content(document_id)
                try:
                    catalog = self._catalog(view, document_id)
                    by_source = (
                        self._catalog_sources(view, document_id, content, catalog)
                        if catalog
                        else {}
                    )
                except (WorkspaceViewError, ValidationError):
                    logger.warning(
                        "document_preview.catalog_unavailable knowledge_base_id=%s "
                        "document_id=%s content_version=%d",
                        knowledge_base_id,
                        document_id,
                        version,
                    )
                    by_source = {}
                images: dict[str, DocumentPreviewImageResponse] = {}
                pieces: list[str] = []
                offset = 0
                for syntax in parse_image_syntax(
                    content, allow_unsafe_targets=True
                ).images:
                    entry = by_source.get(syntax.source)
                    image_id = (
                        entry.image_id
                        if entry
                        else "unavailable_"
                        + hashlib.sha256(syntax.source.encode()).hexdigest()[:24]
                    )
                    if image_id not in images:
                        images[image_id] = DocumentPreviewImageResponse(
                            id=image_id,
                            caption=syntax.caption,
                            status="available" if entry else "unavailable",
                            url=(
                                f"/api/v1/knowledge-bases/{quote(knowledge_base_id, safe='')}/documents/"
                                f"{quote(document_id, safe='')}/images/{image_id}/content?content_version={version}"
                            )
                            if entry
                            else None,
                            width=entry.width if entry else None,
                            height=entry.height if entry else None,
                            orientation=entry.orientation if entry else 1,
                        )
                    pieces.extend(
                        [
                            content[offset : syntax.start],
                            f"![{escape_image_caption(syntax.caption)}](nianlun-document-image:{image_id})",
                            "\n" * content[syntax.start : syntax.end].count("\n"),
                        ]
                    )
                    offset = syntax.end
                pieces.append(content[offset:])
                return DocumentPreviewResponse(
                    content=content,
                    rendered_content="".join(pieces),
                    content_version=version,
                    images=list(images.values()),
                )
        except WorkspaceViewError:
            raise ApiError("文档解析内容不可读", 500) from None

    def read_image(
        self, knowledge_base_id: str, document_id: str, image_id: str, version: int
    ) -> tuple[bytes, str]:
        try:
            with self._view(knowledge_base_id, document_id, version) as (view, _):
                catalog = self._catalog(view, document_id)
                entry = (
                    next(
                        (item for item in catalog.entries if item.image_id == image_id),
                        None,
                    )
                    if catalog
                    else None
                )
                if entry is None:
                    raise ApiError("图片不存在", 404)
                metadata = ImageMetadata.model_validate(
                    entry.model_dump(
                        include={
                            "sha256",
                            "media_type",
                            "size_bytes",
                            "width",
                            "height",
                            "orientation",
                        }
                    )
                )
                store = FilesystemImageBlobStore(
                    view.workspace, profile_id="document-source"
                )
                with store.open(
                    ImageBlobLocation(
                        backend="filesystem",
                        storage_profile_id="document-source",
                        object_key=entry.asset_relpath,
                    ),
                    metadata,
                ) as source:
                    return source.read(), metadata.media_type
        except (WorkspaceViewError, ValidationError, BlobStoreError):
            logger.warning(
                "document_preview.image_unavailable knowledge_base_id=%s document_id=%s image_id=%s",
                knowledge_base_id,
                document_id,
                image_id,
            )
            raise ApiError("图片不存在或暂不可用", 404) from None
