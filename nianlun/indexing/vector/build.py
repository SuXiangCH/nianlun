"""Build a dense-vector index from a workspace.

Supports two modes (design doc §5.3/§5.6):
- Full blue-green (``force=True`` or ``doc_ids is None``): build a staging
  collection, embed all target documents, validate, then atomically rename it
  into place. Used by force rebuilds, embedding-model changes, and the offline CLI.
- Incremental (``doc_ids=[...]``, ``force=False``): operate on the live
  collection -- ``ensure_collection`` + per-document ``delete_by_doc`` + embed +
  insert. Only the dirty documents are re-embedded; no staging/rename.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nianlun.config import get_embedding_model
from nianlun.indexing.vector.build_records import build_records
from nianlun.indexing.vector.config import get_embedding_dim
from nianlun.models.embedding import (
    TextEmbedder,
    build_embedding_client,
    embed_records,
)
from nianlun.indexing.vector.store import DocVectorStore

logger = logging.getLogger(__name__)

BATCH_SIZE = 64
INSERT_BATCH_SIZE = 500
ProgressCallback = Callable[[str, int, int, int], None]


def _load_meta(workspace: Path) -> dict[str, Any]:
    meta_path = workspace / "_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"workspace 缺少 _meta.json: {workspace}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _resolve_document_ids(workspace: Path, doc_ids: list[str] | None) -> list[str]:
    meta = _load_meta(workspace)
    if doc_ids is None:
        return [
            str(doc_id) for doc_id in meta if (workspace / f"{doc_id}.json").is_file()
        ]
    return [
        str(doc_id) for doc_id in doc_ids if (workspace / f"{doc_id}.json").is_file()
    ]


def build_doc_vectors(
    workspace_dir: str | Path,
    *,
    uri: str | None = None,
    token: str | None = None,
    collection_name: str | None = None,
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
    knowledge_base_id: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    allow_env_fallback: bool = True,
    embedder: TextEmbedder | None = None,
    progress_callback: ProgressCallback | None = None,
    doc_ids: list[str] | None = None,
    force: bool = False,
) -> DocVectorStore:
    """Build (or incrementally refresh) a vector collection.

    Args:
        doc_ids: Only embed/insert these documents (incremental). ``None`` = all
            documents in ``_meta.json`` (full build). Mutually exclusive with the
            incremental path: when ``doc_ids is None`` or ``force`` is set, the
            full blue-green staging+publish path is used.
        force: ``True`` forces the full blue-green path (drop+recreate via rename)
            regardless of ``doc_ids``. Used for force rebuilds and model changes.
    """
    workspace = Path(workspace_dir)
    document_ids = _resolve_document_ids(workspace, doc_ids)
    total_documents = len(document_ids)

    def report(
        stage: str,
        completed: int,
        documents_total: int,
        records_processed: int,
    ) -> None:
        if progress_callback is not None:
            progress_callback(stage, completed, documents_total, records_processed)

    report("preparing", 0, total_documents, 0)
    model = embedding_model or get_embedding_model()
    dimension = embedding_dim if embedding_dim is not None else get_embedding_dim()
    client = embedder or build_embedding_client(
        model=model,
        api_key=api_key,
        base_url=base_url,
        dimensions=dimension,
        allow_env_fallback=allow_env_fallback,
    )
    probe = client.embed_query("维度探针")
    if len(probe) != dimension:
        raise ValueError(
            f"embedding 维度不匹配: configured={dimension}, actual={len(probe)}"
        )

    target_store = DocVectorStore(
        uri=uri,
        token=token,
        collection_name=collection_name,
        dimension=dimension,
        knowledge_base_id=knowledge_base_id,
    )

    full_blue_green = force or doc_ids is None
    if full_blue_green:
        return _build_full_blue_green(
            workspace,
            document_ids,
            target_store,
            uri,
            token,
            dimension,
            knowledge_base_id,
            client,
            report,
        )
    return _build_incremental(
        workspace, document_ids, target_store, knowledge_base_id, client, report
    )


def _build_full_blue_green(
    workspace: Path,
    document_ids: list[str],
    target_store: DocVectorStore,
    uri: str | None,
    token: str | None,
    dimension: int,
    knowledge_base_id: str | None,
    client: TextEmbedder,
    report: Callable[[str, int, int, int], None],
) -> DocVectorStore:
    """Staging collection + embed all + publish (blue-green, zero-downtime)."""
    total_documents = len(document_ids)
    staging_collection = (
        f"{target_store.collection[:210]}__building_{uuid.uuid4().hex[:16]}"
    )
    staging_store = DocVectorStore(
        uri=uri,
        token=token,
        collection_name=staging_collection,
        dimension=dimension,
        knowledge_base_id=knowledge_base_id,
    )
    report("creating_collection", 0, total_documents, 0)
    staging_store.create_collection()

    try:
        pending: list[dict[str, Any]] = []
        pending_inserts: list[dict[str, Any]] = []
        total = 0
        records_processed = 0
        for document_number, doc_id in enumerate(document_ids, start=1):
            doc_path = workspace / f"{doc_id}.json"
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
            report("embedding", document_number - 1, total_documents, records_processed)
            pending.extend(build_records(doc, knowledge_base_id=knowledge_base_id))
            while len(pending) >= BATCH_SIZE:
                batch = pending[:BATCH_SIZE]
                del pending[:BATCH_SIZE]
                embedded = embed_records(batch, client)
                pending_inserts.extend(embedded)
                records_processed += len(embedded)
                if len(pending_inserts) >= INSERT_BATCH_SIZE:
                    staging_store.insert(pending_inserts)
                    total += len(pending_inserts)
                    pending_inserts.clear()
            report("embedding", document_number, total_documents, records_processed)
        if pending:
            embedded = embed_records(pending, client)
            pending_inserts.extend(embedded)
            records_processed += len(embedded)
        if pending_inserts:
            staging_store.insert(pending_inserts)
            total += len(pending_inserts)

        report("publishing", total_documents, total_documents, records_processed)
        staging_store.flush()
        staging_store.load()
        target_store.publish_collection(staging_collection)
    except Exception:
        try:
            if staging_store.client.has_collection(staging_collection):
                staging_store.client.drop_collection(staging_collection)
        except Exception as cleanup_exc:
            logger.warning(
                "临时向量 collection 清理失败 %s: %s", staging_collection, cleanup_exc
            )
        raise
    logger.info(
        "[vector_index] 索引构建完成(全量): %s 文档 / %s 记录 -> %s",
        total_documents,
        total,
        target_store.collection,
    )
    return target_store


def _build_incremental(
    workspace: Path,
    document_ids: list[str],
    target_store: DocVectorStore,
    knowledge_base_id: str | None,
    client: TextEmbedder,
    report: Callable[[str, int, int, int], None],
) -> DocVectorStore:
    """Incrementally replace each document after its vectors are ready."""
    total_documents = len(document_ids)
    report("creating_collection", 0, total_documents, 0)
    target_store.ensure_collection()

    total = 0
    records_processed = 0
    for document_number, doc_id in enumerate(document_ids, start=1):
        doc_path = workspace / f"{doc_id}.json"
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        report("embedding", document_number - 1, total_documents, records_processed)
        records = build_records(doc, knowledge_base_id=knowledge_base_id)
        embedded_records: list[dict[str, Any]] = []
        for offset in range(0, len(records), BATCH_SIZE):
            embedded = embed_records(records[offset : offset + BATCH_SIZE], client)
            embedded_records.extend(embedded)
            records_processed += len(embedded)

        # Do not remove the live document until every embedding request succeeded.
        target_store.delete_by_doc(doc_id)
        for offset in range(0, len(embedded_records), INSERT_BATCH_SIZE):
            batch = embedded_records[offset : offset + INSERT_BATCH_SIZE]
            target_store.insert(batch)
            total += len(batch)
        report("embedding", document_number, total_documents, records_processed)

    report("publishing", total_documents, total_documents, records_processed)
    target_store.flush()
    target_store.load()
    logger.info(
        "[vector_index] 增量构建完成: %s 文档 / %s 记录 -> %s",
        total_documents,
        total,
        target_store.collection,
    )
    return target_store


__all__ = ["build_doc_vectors"]
