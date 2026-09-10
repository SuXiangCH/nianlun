"""KnowledgeBase 的基础设施 composition root。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nianlun.indexing.fts.config import FTS_SCHEMA_CHECK_TIMEOUT_SECONDS
from nianlun.indexing.fts.store import CollectionSchemaStatus
from nianlun.indexing.vector.config import get_embedding_dim
from nianlun.indexing.vector.store import DocVectorStore
from nianlun.models.embedding import build_embedding_client
from nianlun.knowledgebase.config import KnowledgeBaseConfig
from nianlun.knowledgebase.core import KnowledgeBase
from nianlun.knowledgebase.semantic_retriever import SemanticDocumentRetriever
from nianlun.knowledgebase.full_text_retriever import FullTextNodeRetriever
from nianlun.knowledgebase.local_scan_retriever import LocalScanNodeRetriever

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
        full_text_retriever = self._create_full_text_retriever()

        return KnowledgeBase(
            workspace_dir=self.config.workspace_dir,
            full_text_retriever=full_text_retriever,
            semantic_document_retriever=self._create_semantic_retriever(
                api_key=api_key,
                base_url=base_url,
                allow_env_fallback=allow_env_fallback,
            ),
            snapshot_relpath=self.config.snapshot_relpath,
            snapshot_manifest_sha256=self.config.snapshot_manifest_sha256,
            knowledge_base_id=self.config.knowledge_base_id,
            content_version=self.config.content_version,
        )

    def _create_full_text_retriever(
        self,
    ) -> FullTextNodeRetriever | LocalScanNodeRetriever:
        if self.config.fts_enabled and self.config.fts_ready:
            try:
                retriever = FullTextNodeRetriever(
                    uri=self.config.milvus_uri,
                    token=self.config.milvus_token,
                    collection_name=self.config.fts_collection,
                    knowledge_base_id=self.config.knowledge_base_id,
                )
                schema_status = retriever.store.schema_status(
                    timeout=FTS_SCHEMA_CHECK_TIMEOUT_SECONDS
                )
                if schema_status is not CollectionSchemaStatus.CURRENT:
                    raise RuntimeError(f"FTS schema 状态为 {schema_status.value}")
                return retriever
            except Exception as exc:
                logger.warning(
                    "FTS 远端索引不可用，切换 committed snapshot 本地扫描: %s", exc
                )
        if (
            self.config.snapshot_relpath is None
            or self.config.snapshot_manifest_sha256 is None
            or self.config.knowledge_base_id is None
            or self.config.content_version is None
        ):
            raise RuntimeError("知识库当前没有可用于本地检索的 committed snapshot")
        return LocalScanNodeRetriever(
            workspace_dir=self.config.workspace_dir,
            snapshot_relpath=self.config.snapshot_relpath,
            snapshot_manifest_sha256=self.config.snapshot_manifest_sha256,
            knowledge_base_id=self.config.knowledge_base_id,
            content_version=self.config.content_version,
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
