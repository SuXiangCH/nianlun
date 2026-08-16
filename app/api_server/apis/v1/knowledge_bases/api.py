"""Knowledge-base management routes."""

from typing import Any

from fastapi import APIRouter, File, Header, Request, UploadFile
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.api_server.apis.v1.schemas import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseUpdateRequest,
)
from app.api_server.common.errors import ApiError, success
from app.api_server.services.container import ApiServices

router = APIRouter(prefix="/api/v1/knowledge-bases", tags=["knowledge-bases"])
MAX_BATCH_UPLOAD_FILES = 50


def _services(request: Request) -> ApiServices:
    return request.app.state.services


async def _read_upload_content(file: UploadFile, max_bytes: int) -> bytes:
    """Read one multipart part while enforcing the per-file upload limit."""
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await file.read(min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ApiError("上传文件超过大小限制", 413)
            chunks.append(chunk)
    finally:
        await file.close()
    return b"".join(chunks)


def _schedule_markdown_indexes(
    services: ApiServices,
    knowledge_base_id: str,
    updated_at: str,
    *,
    fts_enabled: bool,
) -> None:
    """Schedule one incremental pass after one or more Markdown uploads."""
    if fts_enabled:
        services.fts.schedule(knowledge_base_id)
    else:
        services.fts.repository.disable_fts(knowledge_base_id, updated_at)
    services.vector.schedule(knowledge_base_id)


@router.post("")
def create_knowledge_base(
    body: KnowledgeBaseCreateRequest, request: Request
) -> dict[str, Any]:
    item = _services(request).knowledge_bases.create(body)
    return success(item.model_dump(mode="json"))


@router.get("")
def list_knowledge_bases(request: Request) -> dict[str, Any]:
    items = _services(request).knowledge_bases.list()
    return success([item.model_dump(mode="json") for item in items])


@router.get("/{knowledge_base_id}")
def get_knowledge_base(knowledge_base_id: str, request: Request) -> dict[str, Any]:
    item = _services(request).knowledge_bases.get(knowledge_base_id)
    return success(item.model_dump(mode="json"))


@router.patch("/{knowledge_base_id}")
def update_knowledge_base(
    knowledge_base_id: str,
    body: KnowledgeBaseUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    services = _services(request)
    previous = services.knowledge_bases.require_record(knowledge_base_id)
    item = services.knowledge_bases.update(knowledge_base_id, body)
    model_changed = (
        "embedding_model_id" in body.model_fields_set
        and body.embedding_model_id != previous.get("embedding_model_id")
    )
    was_enabled = previous.get("vector_status") != "disabled"
    if body.vector_enabled is True:
        services.vector.schedule(knowledge_base_id, activate=True)
        item = services.knowledge_bases.get(knowledge_base_id)
    elif body.vector_enabled is not False and model_changed and was_enabled:
        services.vector.schedule(knowledge_base_id, force=True, activate=True)
        item = services.knowledge_bases.get(knowledge_base_id)
    return success(item.model_dump(mode="json"))


@router.delete("/{knowledge_base_id}")
def delete_knowledge_base(knowledge_base_id: str, request: Request) -> dict[str, Any]:
    services = _services(request)
    item = services.knowledge_bases.delete(knowledge_base_id)
    services.fts.delete_collection(item.get("fts_collection"), knowledge_base_id)
    services.vector.delete_collection(item.get("vector_collection"), knowledge_base_id)
    return success(None)


@router.post("/{knowledge_base_id}/documents")
async def upload_document(
    knowledge_base_id: str,
    request: Request,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 128:
        raise ApiError("Idempotency-Key 长度必须为 1 到 128", 400)
    max_bytes = request.app.state.settings.max_upload_bytes
    filename = file.filename or "document.md"
    content = await _read_upload_content(file, max_bytes)
    item = await run_in_threadpool(
        _services(request).documents.upload,
        knowledge_base_id,
        filename,
        content,
        file.content_type,
        idempotency_key,
    )
    services = _services(request)
    if filename.lower().endswith(".md"):
        _schedule_markdown_indexes(
            services,
            knowledge_base_id,
            item.updated_at.isoformat(),
            fts_enabled=request.app.state.settings.fts_enabled,
        )
    refreshed = _services(request).knowledge_bases.get(knowledge_base_id)
    refreshed = refreshed.model_copy(
        update={
            "document_id": item.document_id,
            "idempotent_replay": item.idempotent_replay,
        }
    )
    return success(refreshed.model_dump(mode="json"))


@router.post("/{knowledge_base_id}/documents/batch")
async def upload_documents_batch(
    knowledge_base_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """Upload several files while keeping each document's lifecycle independent."""
    if not files:
        raise ApiError("上传文件不能为空")
    if len(files) > MAX_BATCH_UPLOAD_FILES:
        raise ApiError(f"单次最多上传 {MAX_BATCH_UPLOAD_FILES} 个文件", 413)

    services = _services(request)
    max_bytes = request.app.state.settings.max_upload_bytes
    results: list[dict[str, Any]] = []
    markdown_updated_at: str | None = None
    for file in files:
        filename = file.filename or "document.md"
        try:
            content = await _read_upload_content(file, max_bytes)
            item = await run_in_threadpool(
                services.documents.upload,
                knowledge_base_id,
                filename,
                content,
                file.content_type,
                None,
            )
        except ApiError as exc:
            results.append(
                {
                    "filename": filename,
                    "ok": False,
                    "status_code": exc.status_code,
                    "error": exc.message,
                }
            )
            continue
        except Exception:
            results.append(
                {
                    "filename": filename,
                    "ok": False,
                    "status_code": 500,
                    "error": "上传失败",
                }
            )
            continue

        results.append(
            {
                "filename": filename,
                "ok": True,
                "document_id": item.document_id,
                "idempotent_replay": item.idempotent_replay,
            }
        )
        if filename.lower().endswith(".md"):
            markdown_updated_at = item.updated_at.isoformat()

    if markdown_updated_at is not None:
        _schedule_markdown_indexes(
            services,
            knowledge_base_id,
            markdown_updated_at,
            fts_enabled=request.app.state.settings.fts_enabled,
        )
    refreshed = services.knowledge_bases.get(knowledge_base_id)
    return success(
        {"knowledge_base": refreshed.model_dump(mode="json"), "files": results}
    )


@router.get("/{knowledge_base_id}/documents")
def list_documents(knowledge_base_id: str, request: Request) -> dict[str, Any]:
    items = _services(request).documents.list_documents(knowledge_base_id)
    return success(items)


@router.get("/{knowledge_base_id}/documents/{document_id}")
def get_document(
    knowledge_base_id: str, document_id: str, request: Request
) -> dict[str, Any]:
    item = _services(request).documents.get_document(knowledge_base_id, document_id)
    return success(item)


@router.post("/{knowledge_base_id}/documents/{document_id}/retry")
def retry_document(
    knowledge_base_id: str, document_id: str, request: Request
) -> dict[str, Any]:
    item = _services(request).documents.retry_document(knowledge_base_id, document_id)
    return success(item)


@router.delete("/{knowledge_base_id}/documents/{document_id}")
def delete_document(
    knowledge_base_id: str, document_id: str, request: Request
) -> dict[str, Any]:
    _services(request).documents.delete_document(knowledge_base_id, document_id)
    return success(None)


@router.get("/{knowledge_base_id}/documents/{document_id}/content")
def get_document_content(
    knowledge_base_id: str, document_id: str, request: Request
) -> PlainTextResponse:
    content = _services(request).documents.read_content(knowledge_base_id, document_id)
    return PlainTextResponse(content, media_type="text/markdown")


@router.get("/{knowledge_base_id}/documents/{document_id}/tree")
def get_document_tree(
    knowledge_base_id: str, document_id: str, request: Request
) -> dict[str, Any]:
    tree = _services(request).documents.read_tree(knowledge_base_id, document_id)
    return success(tree)


@router.post("/{knowledge_base_id}/fts")
def rebuild_fts(knowledge_base_id: str, request: Request) -> dict[str, Any]:
    """Queue a full FTS rebuild for one knowledge base."""
    _services(request).fts.schedule(knowledge_base_id, force=True)
    response = _services(request).knowledge_bases.get(knowledge_base_id)
    return success(response.model_dump(mode="json"))


@router.post("/{knowledge_base_id}/vector")
def rebuild_vector(knowledge_base_id: str, request: Request) -> dict[str, Any]:
    """Queue a full vector-index rebuild for one knowledge base."""
    services = _services(request)
    knowledge_base = services.knowledge_bases.require_record(knowledge_base_id)
    model_id = str(knowledge_base.get("embedding_model_id") or "").strip()
    if not model_id:
        raise ApiError(
            "请先在知识库中选择 Embedding 模型",
            409,
        )
    services.models.embedding_runtime_config(model_id, strict=True)
    services.vector.schedule(knowledge_base_id, force=True, activate=True)
    response = services.knowledge_bases.get(knowledge_base_id)
    return success(response.model_dump(mode="json"))


__all__ = ["router"]
