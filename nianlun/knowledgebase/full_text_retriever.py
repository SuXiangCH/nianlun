"""运行时节点全文检索适配器。

保持 Agent 层只依赖一个很小的 ``search`` 接口，Milvus 细节继续留在
``indexing.fts`` 包中。
"""

from __future__ import annotations

from typing import Any

from nianlun.indexing.fts.store import NodeFtsStore


class FullTextNodeRetriever:
    """NodeFtsStore 的轻量运行时适配器。"""

    def __init__(
        self,
        *,
        uri: str | None = None,
        token: str | None = None,
        collection_name: str | None = None,
        knowledge_base_id: str | None = None,
    ) -> None:
        self.store = NodeFtsStore(
            uri=uri,
            token=token,
            collection_name=collection_name,
            knowledge_base_id=knowledge_base_id,
        )

    def search(
        self,
        query: str,
        limit: int = 512,
        doc_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """传入自然语言 query，返回 Milvus 原始命中。"""
        return self.store.search(query, limit=limit, doc_ids=doc_ids)


__all__ = ["FullTextNodeRetriever"]
