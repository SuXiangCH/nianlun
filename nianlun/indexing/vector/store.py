"""Milvus dense-vector collection and search wrapper."""

from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from nianlun.indexing.vector.config import (
    VECTOR_HNSW_EF_CONSTRUCTION,
    VECTOR_HNSW_M,
    VECTOR_SEARCH_EF,
    get_embedding_dim,
    get_milvus_token,
    get_milvus_uri,
    get_vector_collection,
)

try:
    from pymilvus import DataType, MilvusClient
except ImportError as exc:  # pragma: no cover - dependency is declared
    DataType = None
    MilvusClient = None
    _PYMILVUS_IMPORT_ERROR: Exception | None = exc
else:
    _PYMILVUS_IMPORT_ERROR = None


logger = logging.getLogger(__name__)


def ensure_pymilvus() -> None:
    if _PYMILVUS_IMPORT_ERROR is None:
        return
    logger.error("未安装 pymilvus。请运行: uv sync --all-groups")
    raise _PYMILVUS_IMPORT_ERROR


def _output_fields() -> list[str]:
    return [
        "doc_id",
        "doc_name",
        "source_type",
        "node_id",
        "title",
        "line_num",
        "knowledge_base_id",
    ]


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class DocVectorStore:
    """Manage a collection of already-vectorized records.

    This class deliberately has no embedding client or model configuration.
    Callers must vectorize source text before ``insert`` and vectorize queries
    before ``search``.
    """

    def __init__(
        self,
        uri: str | None = None,
        token: str | None = None,
        collection_name: str | None = None,
        dimension: int | None = None,
        knowledge_base_id: str | None = None,
    ) -> None:
        ensure_pymilvus()
        self.client = MilvusClient(
            uri=uri or get_milvus_uri(),
            token=(token if token is not None else get_milvus_token()) or "",
        )
        self.collection = collection_name or get_vector_collection()
        self.dimension = dimension if dimension is not None else get_embedding_dim()
        if self.dimension <= 0:
            raise ValueError("向量维度必须是正整数")
        self.knowledge_base_id = knowledge_base_id
        self._loaded = False

    def create_collection(self) -> None:
        """Drop and recreate the collection with a Milvus 2.6 HNSW index."""
        if self.client.has_collection(self.collection):
            self.client.drop_collection(self.collection)

        schema = self.client.create_schema(auto_id=True, enable_dynamic_field=False)
        schema.add_field("emb_pk", DataType.INT64, is_primary=True)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        schema.add_field("doc_name", DataType.VARCHAR, max_length=512)
        schema.add_field("source_type", DataType.VARCHAR, max_length=16)
        schema.add_field("node_id", DataType.VARCHAR, max_length=16, nullable=True)
        schema.add_field("title", DataType.VARCHAR, max_length=512, nullable=True)
        schema.add_field("line_num", DataType.INT64, nullable=True)
        schema.add_field(
            "knowledge_base_id",
            DataType.VARCHAR,
            max_length=256,
            nullable=True,
        )
        schema.add_field(
            "vector",
            DataType.FLOAT_VECTOR,
            dim=self.dimension,
        )

        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={
                "M": VECTOR_HNSW_M,
                "efConstruction": VECTOR_HNSW_EF_CONSTRUCTION,
            },
        )
        self.client.create_collection(
            collection_name=self.collection,
            schema=schema,
            index_params=index_params,
        )
        self._loaded = False

    def ensure_collection(self) -> bool:
        """Ensure the collection exists, creating it if missing.

        Incremental path (design §5.3): avoid dropping an existing collection so
        other documents' vectors are preserved. Returns ``True`` if the collection
        already existed (can increment); ``False`` if it was just created (caller
        should treat all documents as dirty and write them in full).
        """
        if self.client.has_collection(self.collection):
            return True
        self.create_collection()
        return False

    def validate_collection(self) -> None:
        """Validate the collection contract before exposing vector search.

        Older vector collections do not contain the current isolation metadata and
        must be rebuilt. Failing at startup is clearer and safer than returning
        cross-workspace results or failing on the first user query.
        """
        description = cast(
            dict[str, Any], self.client.describe_collection(self.collection)
        )
        fields = {
            str(field.get("name")): field
            for field in description.get("fields", [])
            if isinstance(field, dict)
        }
        required = {
            "doc_id",
            "doc_name",
            "source_type",
            "node_id",
            "title",
            "line_num",
            "knowledge_base_id",
            "vector",
        }
        missing = sorted(required - fields.keys())
        if missing:
            raise RuntimeError(
                f"向量 collection 缺少新 schema 字段: {', '.join(missing)}；请重建索引"
            )
        vector_params = fields["vector"].get("params", {})
        actual_dimension = (
            vector_params.get("dim") if isinstance(vector_params, dict) else None
        )
        if actual_dimension != self.dimension:
            raise RuntimeError(
                f"向量 collection 维度不匹配: configured={self.dimension}, "
                f"actual={actual_dimension}；请检查 EMBEDDING_DIM 或重建索引"
            )

    def publish_collection(self, staging_collection: str) -> None:
        """Publish a completely built staging collection while preserving recovery.

        Milvus collection names cannot be overwritten by rename. The target is
        therefore moved to a unique backup first, then the staging collection is
        renamed into place. If the second rename fails, the old target is restored.
        """
        if not self.client.has_collection(staging_collection):
            raise RuntimeError(f"临时向量 collection 不存在: {staging_collection}")

        backup_collection = None
        target_exists = self.client.has_collection(self.collection)
        if target_exists:
            backup_collection = (
                f"{self.collection[:210]}__backup_{uuid.uuid4().hex[:16]}"
            )
            self.client.rename_collection(self.collection, backup_collection)
        try:
            self.client.rename_collection(staging_collection, self.collection)
        except Exception:
            if backup_collection and self.client.has_collection(backup_collection):
                self.client.rename_collection(backup_collection, self.collection)
            raise

        if backup_collection and self.client.has_collection(backup_collection):
            try:
                self.client.drop_collection(backup_collection)
            except Exception as exc:
                # The new collection is already published; stale backup cleanup
                # can be retried by an operator without invalidating the index.
                logger.warning(
                    "旧向量 collection 清理失败 %s: %s", backup_collection, exc
                )
        self._loaded = True

    def insert(self, records: list[dict[str, Any]]) -> None:
        """Insert records that already contain a vector of the configured size."""
        if not records:
            return
        for index, record in enumerate(records):
            if "embed_text" in record:
                raise ValueError(
                    "向量存储不接受 embed_text；请先通过 embedding 层生成 vector"
                )
            vector = record.get("vector")
            if not isinstance(vector, list) or len(vector) != self.dimension:
                actual = len(vector) if isinstance(vector, list) else 0
                raise ValueError(
                    f"第 {index} 条记录向量维度不匹配: "
                    f"expected={self.dimension}, actual={actual}"
                )
        self.client.insert(collection_name=self.collection, data=records)

    def delete_by_doc(self, doc_id: str) -> None:
        """删除某文档的全部向量记录（按 ``doc_id`` 过滤批量删）。

        增量删除路径使用：定向移除被删文档的向量，无需全量重建。共享 collection 下
        同时带 ``knowledge_base_id`` 保险（``doc_id`` 本身是全局唯一 UUID）。
        """
        filters = [f'doc_id == "{_escape_filter_value(doc_id)}"']
        if self.knowledge_base_id is not None:
            filters.append(
                f'knowledge_base_id == "{_escape_filter_value(self.knowledge_base_id)}"'
            )
        self.client.delete(
            collection_name=self.collection, filter=" and ".join(filters)
        )

    def flush(self) -> None:
        self.client.flush(self.collection)

    def load(self) -> None:
        self.client.load_collection(self.collection)
        self._loaded = True

    def search(self, vector: list[float], limit: int = 512) -> list[dict[str, Any]]:
        """Search using an already-vectorized query and return candidate records.

        Node-level deduplication and the per-document five-node cap belong to the
        post-processing layer, just as they do for FTS. Keeping this layer raw
        gives the router enough candidates to distribute results across documents.
        """
        if not vector:
            return []
        if len(vector) != self.dimension:
            raise ValueError(
                f"查询向量维度不匹配: expected={self.dimension}, actual={len(vector)}"
            )
        if limit <= 0:
            return []
        if not self._loaded:
            self.load()

        search_kwargs: dict[str, Any] = {}
        if self.knowledge_base_id is not None:
            search_kwargs["filter"] = (
                f'knowledge_base_id == "{_escape_filter_value(self.knowledge_base_id)}"'
            )

        result = self.client.search(
            collection_name=self.collection,
            data=[vector],
            anns_field="vector",
            limit=limit,
            output_fields=_output_fields(),
            search_params={
                "metric_type": "COSINE",
                # Milvus HNSW requires ef >= k (the search limit). The router
                # deliberately fetches a larger candidate set for per-document
                # node capping, so the configured minimum must be raised here.
                "params": {"ef": max(VECTOR_SEARCH_EF, limit)},
            },
            **search_kwargs,
        )
        hits: list[dict[str, Any]] = []
        for group in result:
            for hit in group:
                entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
                item = {field: entity.get(field) for field in _output_fields()}
                item["score"] = hit.get("distance")
                hits.append(item)
        return sorted(
            hits,
            key=lambda item: item["score"] or 0,
            reverse=True,
        )[:limit]


def _smoke_search() -> None:
    """Run read-only semantic queries against an existing vector collection.

    Usage: ``python -m nianlun.indexing.vector.store``. This function never
    creates, drops, inserts into, or rebuilds a collection.
    """
    import sys

    from nianlun.config import get_embedding_model
    from nianlun.indexing.vector.config import (
        VECTOR_DERIVE_LIMIT,
        VECTOR_NODE_PER_DOC,
    )
    from nianlun.models.embedding import build_embedding_client
    from nianlun.indexing.fts.postprocess import postprocess_node_hits, top_doc_ids

    store = DocVectorStore()
    if not store.client.has_collection(store.collection):
        print(
            f"collection {store.collection!r} 不存在；请先构建向量索引", file=sys.stderr
        )
        sys.exit(1)

    embedder = build_embedding_client(model=get_embedding_model())
    print(f"collection: {store.collection}（每文档最多 {VECTOR_NODE_PER_DOC} 个节点）")
    for query in ("test", "test2", "测试"):
        hits = store.search(embedder.embed_query(query), limit=VECTOR_DERIVE_LIMIT)
        nodes = [hit for hit in hits if hit.get("node_id")]
        distinct_nodes = {(hit.get("doc_id"), hit.get("node_id")) for hit in nodes}
        docs = top_doc_ids(hits, doc_top_n=20)
        selected_nodes = [hit for hit in nodes if hit.get("doc_id") in docs]
        processed = postprocess_node_hits(
            selected_nodes, per_doc_cap=VECTOR_NODE_PER_DOC
        )
        print(f"\n=== {query!r} ===")
        print(
            f"  原始 {len(nodes)} 条节点命中（distinct node {len(distinct_nodes)}），"
            f"文档 {len(docs)} 个；后处理 {len(processed)} 条"
        )
        for hit in processed[:10]:
            print(
                f"  {hit['score']:6.3f} node={hit['node_id']:<8} "
                f"doc={str(hit['doc_id'])[:8]} {hit['doc_name'][:32]}"
            )
        doc_scores: dict[str, tuple[float, str]] = {}
        for h in hits:
            did = str(h.get("doc_id") or "")
            sc = h.get("score") or 0
            if did and (did not in doc_scores or sc > doc_scores[did][0]):
                doc_scores[did] = (sc, str(h.get("doc_name") or ""))
        print("  文档 top10（每文档一条，取最高分节点）：")
        for did in docs[:10]:
            sc, name = doc_scores[did]
            print(f"  {sc:6.3f} doc={did[:8]} {name[:32]}")


if __name__ == "__main__":
    _smoke_search()


__all__ = ["DocVectorStore", "ensure_pymilvus"]
