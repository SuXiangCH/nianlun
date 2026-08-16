"""Composition root for API services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.repositories import SQLiteChatRepository, SQLiteMetadataRepository
from app.api_server.services.application_service import ApplicationService
from app.api_server.services.chat_service import ChatService
from app.api_server.services.document_ingestion_service import DocumentIngestionService
from app.api_server.services.fts_index_service import FTSIndexService
from app.api_server.services.knowledge_base_service import KnowledgeBaseService
from app.api_server.services.model_config_service import ModelConfigService
from app.api_server.services.vector_index_service import VectorIndexService


@dataclass
class ApiServices:
    knowledge_bases: KnowledgeBaseService
    documents: DocumentIngestionService
    fts: FTSIndexService
    vector: VectorIndexService
    applications: ApplicationService
    chat: ChatService
    models: ModelConfigService


def build_services(settings: ApiServerSettings) -> ApiServices:
    database_path = settings.database_path or settings.data_dir / "nianlun.sqlite3"
    factory = SQLiteConnectionFactory(
        database_path,
        timeout_seconds=settings.database_timeout_seconds,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    )
    initialize_database(factory)
    repository = SQLiteMetadataRepository(factory)
    chat_repository = SQLiteChatRepository(factory)
    chat_repository.fail_pending_messages(datetime.now(timezone.utc))
    models = ModelConfigService(repository)
    knowledge_bases = KnowledgeBaseService(repository, settings.workspace_root, models)
    fts = FTSIndexService(repository, knowledge_bases.require_record, settings)
    vector = VectorIndexService(
        repository,
        knowledge_bases.require_record,
        models.embedding_runtime_config,
        settings,
    )
    documents = DocumentIngestionService(
        repository,
        knowledge_bases,
        models,
        settings,
        fts_schedule=fts.schedule,
        vector_schedule=vector.schedule,
    )
    applications = ApplicationService(
        repository,
        knowledge_bases.require_record,
        provider_resolver=settings.resolve_provider,
        fts_enabled=settings.fts_enabled,
        milvus_uri=settings.milvus_uri,
        milvus_token=settings.milvus_token,
        vector_enabled=settings.vector_enabled,
        vector_collection=settings.vector_collection,
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
        model_config_service=models,
    )
    knowledge_bases.reconcile()
    documents.recover_workspace_documents()
    fts.recover_pending()
    vector.recover_pending()
    documents.recover()
    return ApiServices(
        knowledge_bases=knowledge_bases,
        documents=documents,
        fts=fts,
        vector=vector,
        applications=applications,
        chat=ChatService(applications, chat_repository),
        models=models,
    )


__all__ = ["ApiServices", "build_services"]
