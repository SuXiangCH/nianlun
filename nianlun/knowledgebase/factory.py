"""KnowledgeBase 的基础设施 composition root。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nianlun.indexing.vector.config import get_embedding_dim
from nianlun.models.embedding import build_embedding_client
from nianlun.indexing.vector.store import DocVectorStore
from nianlun.knowledgebase.config import KnowledgeBaseConfig
from nianlun.knowledgebase.core import KnowledgeBase
from nianlun.knowledgebase.semantic_retriever import SemanticDocumentRetriever
from nianlun.knowledgebase.full_text_retriever import FullTextNodeRetriever

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KnowledgeBaseFactory:
    """根据应用配置创建 workspace、FTS 和语义检索适配器。"""

    config: KnowledgeBaseConfig

    def create(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        allow_env_fallback: bool,
    ) -> KnowledgeBase:
        if not self.config.fts_enabled:
            raise RuntimeError("全文检索未启用。")

        full_text_retriever = FullTextNodeRetriever(
            uri=self.config.milvus_uri,
            token=self.config.milvus_token,
            collection_name=self.config.fts_collection,
            knowledge_base_id=self.config.knowledge_base_id,
        )
        if not full_text_retriever.store.client.has_collection(
            full_text_retriever.store.collection
        ):
            raise RuntimeError(
                f"Milvus collection 不存在: {full_text_retriever.store.collection}"
            )

        return KnowledgeBase(
            workspace_dir=self.config.workspace_dir,
            full_text_retriever=full_text_retriever,
            semantic_document_retriever=self._create_semantic_retriever(
                api_key=api_key,
                base_url=base_url,
                allow_env_fallback=allow_env_fallback,
            ),
        )

    def _create_semantic_retriever(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        allow_env_fallback: bool,
    ) -> SemanticDocumentRetriever | None:
        if not self.config.vector_enabled:
            return None
        try:
            dimension = self.config.embedding_dim or get_embedding_dim()
            embedder = build_embedding_client(
                model=self.config.embedding_model,
                api_key=self.config.embedding_api_key or api_key,
                base_url=self.config.embedding_base_url or base_url,
                dimensions=dimension,
                allow_env_fallback=allow_env_fallback,
            )
            store = DocVectorStore(
                uri=self.config.milvus_uri,
                token=self.config.milvus_token,
                collection_name=self.config.vector_collection,
                dimension=dimension,
                knowledge_base_id=self.config.knowledge_base_id,
            )
            if not store.client.has_collection(store.collection):
                raise RuntimeError(f"Milvus collection 不存在: {store.collection}")
            store.validate_collection()
            return SemanticDocumentRetriever(store, embedder)
        except Exception as exc:
            logger.warning("向量检索不可用，Agent 不注册语义文档工具: %s", exc)
            return None


__all__ = ["KnowledgeBaseFactory"]
