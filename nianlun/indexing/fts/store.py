"""Milvus FTS collection 封装：建表 / insert / search。

``NodeFtsStore`` 封装 ``MilvusClient`` 的 collection 生命周期与检索，供离线 build 与（未来）
运行时检索复用。``pymilvus`` 惰性导入（缺时不阻断 import，调用时 ``ensure_pymilvus`` 报错）。

schema 为多记录单字段：``text``（BM25 输入，混合语言 analyzer）+ ``sparse``
（BM25 输出），``source_type`` 区分三源，``node_id``/``title``/``line_num`` 可空（``doc_desc`` 记录空）。
整体设计见 ``docs/architecture/fts_design.md``。
"""

from __future__ import annotations

import logging
from typing import Any

from nianlun.indexing.fts.config import (
    TEXT_MAX_BYTES,
    get_fts_analyzer_params,
    get_milvus_token,
    get_milvus_uri,
    get_node_fts_collection,
)

try:
    from pymilvus import DataType, Function, FunctionType, MilvusClient
except ImportError as exc:  # pragma: no cover - 缺 pymilvus 时给出明确报错
    MilvusClient = None
    DataType = None
    Function = None
    FunctionType = None
    _PYMILVUS_IMPORT_ERROR: Exception | None = exc
else:
    _PYMILVUS_IMPORT_ERROR = None


logger = logging.getLogger(__name__)


def ensure_pymilvus() -> None:
    """检查 pymilvus 依赖；缺失时记录安装提示并抛原异常。"""
    if _PYMILVUS_IMPORT_ERROR is None:
        return
    logger.error("未安装 pymilvus。请运行: uv sync --all-groups")
    raise _PYMILVUS_IMPORT_ERROR


def _output_fields() -> list[str]:
    """search 返回字段（不含 ``text``：不泄正文，保"定位后现取"）。"""
    return ["doc_id", "doc_name", "source_type", "node_id", "title", "line_num"]


def query_variants(query: str) -> list[str]:
    """返回兼容旧 collection 的英文大小写变体。

    早期 collection 使用 ``chinese`` analyzer，它不会统一英文大小写；旧索引中可能
    同时存在 ``API``、``Api`` 和 ``api`` 形式。查询侧兼容可以让现有索引立即获得
    大小写不敏感的英文检索能力。
    """
    if not any(char.isascii() and char.isalpha() for char in query):
        return [query]

    variants: list[str] = []
    for variant in (query, query.casefold(), query.upper(), query.title()):
        if variant not in variants:
            variants.append(variant)
    return variants


class NodeFtsStore:
    """节点 FTS collection 的建表 / 插入 / 检索封装。"""

    def __init__(
        self,
        uri: str | None = None,
        token: str | None = None,
        collection_name: str | None = None,
        knowledge_base_id: str | None = None,
    ) -> None:
        ensure_pymilvus()
        self.client = MilvusClient(
            uri=uri or get_milvus_uri(),
            token=(token if token is not None else get_milvus_token()) or "",
        )
        self.collection = collection_name or get_node_fts_collection()
        self.knowledge_base_id = knowledge_base_id
        self._loaded = (
            False  # 是否已对当前 collection 发过 load（避免每次 search 重发）
        )

    def create_collection(self) -> None:
        """drop if exists + 建 schema(BM25 function + mixed-language analyzer) + SPARSE_INVERTED_INDEX。

        BM25 function 与 analyzer 必须建表时定义、不可后加（Milvus 约束），故刷新 = drop+recreate。
        """
        if self.client.has_collection(self.collection):
            self.client.drop_collection(self.collection)

        schema = self.client.create_schema(auto_id=True, enable_dynamic_field=False)
        schema.add_field("emb_pk", DataType.INT64, is_primary=True)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        schema.add_field(
            "knowledge_base_id",
            DataType.VARCHAR,
            max_length=256,
            nullable=True,
        )
        schema.add_field("doc_name", DataType.VARCHAR, max_length=512)
        schema.add_field("source_type", DataType.VARCHAR, max_length=16)
        schema.add_field("node_id", DataType.VARCHAR, max_length=16, nullable=True)
        schema.add_field("title", DataType.VARCHAR, max_length=512, nullable=True)
        schema.add_field("line_num", DataType.INT64, nullable=True)
        schema.add_field(
            "text",
            DataType.VARCHAR,
            max_length=TEXT_MAX_BYTES,
            enable_analyzer=True,
            analyzer_params=get_fts_analyzer_params(),
        )
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="text_bm25",
                input_field_names=["text"],
                output_field_names=["sparse"],
                function_type=FunctionType.BM25,
            )
        )

        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={
                "inverted_index_algo": "DAAT_MAXSCORE",
                "bm25_k1": 1.2,
                "bm25_b": 0.75,
            },
        )
        self.client.create_collection(
            collection_name=self.collection,
            schema=schema,
            index_params=index_params,
        )
        self._loaded = False  # drop+recreate 后 collection 已变，旧 load 状态失效

    def ensure_collection(self) -> bool:
        """确保 collection 存在：缺失则建表，已存在则不动。

        增量路径使用（设计文档 §5.3）：避免 drop 已有 collection，保留其余文档的记录。
        Returns:
            ``True`` 表示 collection 已存在（可增量）；``False`` 表示刚新建（调用方应将
            全部文档判脏后全量写入）。
        """
        if self.client.has_collection(self.collection):
            return True
        self.create_collection()
        return False

    def insert(self, records: list[dict[str, Any]]) -> None:
        """插入三源记录（``text`` 由 Milvus 自动 tokenize 建 ``sparse``）。

        ``emb_pk``（auto_id）与 ``sparse``（function 输出）由 Milvus 生成，records 不含。
        本期 drop+recreate 后 insert（非 upsert，auto_id 主键下 upsert 会重复）。
        """
        if not records:
            return
        self.client.insert(collection_name=self.collection, data=records)

    def delete_by_doc(self, doc_id: str) -> None:
        """删除某文档的全部三源记录（按 ``doc_id`` 过滤批量删）。

        增量删除路径使用：定向移除被删文档在 collection 中的记录，无需 drop+recreate。
        ``auto_id`` 主键下按 ``doc_id`` 过滤的 ``delete`` 受官方支持（见设计文档 §4）。
        共享 collection 下同时带 ``knowledge_base_id`` 保险（``doc_id`` 本身是全局唯一 UUID）。
        """
        escaped_doc = doc_id.replace("\\", "\\\\").replace('"', '\\"')
        filters = [f'doc_id == "{escaped_doc}"']
        if self.knowledge_base_id is not None:
            escaped_kb = self.knowledge_base_id.replace("\\", "\\\\").replace(
                '"', '\\"'
            )
            filters.append(f'knowledge_base_id == "{escaped_kb}"')
        self.client.delete(
            collection_name=self.collection, filter=" and ".join(filters)
        )

    def flush(self) -> None:
        """封存已插入段（seal growing segment），使 BM25 倒排索引在段上建好。

        **必须**在 insert 全部完成后、search 前调用一次，否则刚插入的数据检索不到
        （BM25 稀疏索引需段封存后才可查）。build 流程在 load 前 flush 一次。
        """
        self.client.flush(self.collection)

    def load(self) -> None:
        """load collection 到内存后方可 search（幂等）。

        成功后置 ``_loaded``，``search`` 据此跳过重复 load（官方模式：load 一次、search 多次）。
        """
        self.client.load_collection(self.collection)
        self._loaded = True

    def search(
        self,
        query: str,
        limit: int = 512,
        doc_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 检索，兼容旧 collection 的英文大小写差异。

        Milvus 自动使用 collection analyzer 分词。对于包含英文字母的 query，会额外
        搜索大小写变体，并按结果实体去重、保留最高分，使旧的大小写敏感 collection
        也能获得大小写不敏感的检索效果。

        Returns:
            ``[{doc_id, doc_name, source_type, node_id, title, line_num, score}]``，
            按 BM25 分数降序。``node_id`` 为 ``None`` 的命中来自 ``doc_desc`` 记录（文档级）。
        """
        if not self._loaded:  # 仅首次 load，后续 search 不重发（幂等但省往返）
            self.load()
        search_kwargs = {}
        filters: list[str] = []
        if self.knowledge_base_id is not None:
            escaped_id = self.knowledge_base_id.replace("\\", "\\\\").replace(
                '"', '\\"'
            )
            filters.append(f'knowledge_base_id == "{escaped_id}"')
        if doc_ids is not None:
            if not doc_ids:
                return []
            escaped_doc_ids = [
                f'"{doc_id.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                for doc_id in doc_ids
            ]
            filters.append(f"doc_id in [{', '.join(escaped_doc_ids)}]")
        if filters:
            search_kwargs["filter"] = " and ".join(filters)
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for variant in query_variants(query):
            res = self.client.search(
                collection_name=self.collection,
                data=[variant],
                anns_field="sparse",
                limit=limit,
                output_fields=_output_fields(),
                **search_kwargs,
            )
            for hits in res:
                for hit in hits:
                    entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
                    item = {
                        "doc_id": entity.get("doc_id"),
                        "doc_name": entity.get("doc_name"),
                        "source_type": entity.get("source_type"),
                        "node_id": entity.get("node_id"),
                        "title": entity.get("title"),
                        "line_num": entity.get("line_num"),
                        "score": hit.get("distance"),
                    }
                    key = tuple(item[field] for field in _output_fields())
                    current = merged.get(key)
                    if current is None or (item["score"] or 0) > (current["score"] or 0):
                        merged[key] = item

        return sorted(
            merged.values(),
            key=lambda item: item["score"] or 0,
            reverse=True,
        )[:limit]


def _smoke_search() -> None:
    """搜索冒烟：对已建好的 collection 跑几个查询，打印【原始命中 vs 后处理命中】对比。

    后处理 = 节点去重（同节点 node_text+node_summary 双命中合并）+ 每文档限额
    （``NODE_PER_DOC``，防霸榜）。用真实数据验证 ``postprocess.postprocess_node_hits``
    的效果（只读，不建/删）。

    用法::

        MILVUS_URI=http://localhost:19530 python -m nianlun.indexing.fts.store

    需先建索引（``python -m nianlun.indexing.fts.cli --workspace <dir>``）。
    """
    import sys

    from nianlun.indexing.fts.config import DOC_DERIVE_LIMIT, NODE_MATCH_LIMIT, NODE_PER_DOC
    from nianlun.indexing.fts.postprocess import postprocess_node_hits

    s = NodeFtsStore()
    if not s.client.has_collection(s.collection):
        print(
            f"collection {s.collection!r} 不存在；先建索引："
            f"python -m nianlun.indexing.fts.cli --workspace <dir>",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"collection: {s.collection}（cap_per_doc={NODE_PER_DOC}）")
    queries = (
        "test",
        "测试"
    )
    for q in queries:
        hits = s.search(q, limit=DOC_DERIVE_LIMIT)
        node_hits = [h for h in hits if h.get("node_id")]
        distinct_nodes = {(h["doc_id"], h["node_id"]) for h in node_hits}
        processed = postprocess_node_hits(
            node_hits, per_doc_cap=NODE_PER_DOC, limit=NODE_MATCH_LIMIT
        )

        print(f"\n=== {q!r} ===")
        print(
            f"  原始 {len(node_hits)} 条节点命中（distinct node {len(distinct_nodes)}，"
            f"差额 {len(node_hits) - len(distinct_nodes)} 为同节点多源重复）"
        )
        print(f"  后处理 {len(processed)} 条（去重 + cap={NODE_PER_DOC}）")
        for h in processed[:10]:
            nid = h["node_id"]
            did = (h["doc_id"] or "")[:8]
            ms = "+".join(h.get("matched_sources", []))
            print(
                f"  {h['score']:6.2f}  {ms:24}  node={nid:<5}  doc={did}  {h['doc_name'][:32]}"
            )


if __name__ == "__main__":
    _smoke_search()
