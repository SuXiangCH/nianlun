"""Composition root for API services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import ExitStack
from collections.abc import Callable

from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.repositories import SQLiteChatRepository, SQLiteMetadataRepository
from app.api_server.services.application_service import ApplicationService
from app.api_server.repositories.publishing import PublishingRepository
from app.api_server.services.application_release_service import (
    ApplicationReleaseService,
)
from app.api_server.services.application_invocation_service import (
    ApplicationInvocationService,
)
from app.api_server.services.application_wecom_service import ApplicationWecomService
from app.api_server.services.chat_service import ChatService
from app.api_server.services.document_ingestion_service import DocumentIngestionService
from app.api_server.services.document_preview_service import DocumentPreviewService
from app.api_server.services.fts_index_service import FTSIndexService
from app.api_server.services.gateway_control import GatewayControlClient
from app.api_server.services.knowledge_base_service import KnowledgeBaseService
from app.api_server.services.model_config_service import ModelConfigService
from app.api_server.services.vector_index_service import VectorIndexService
from app.api_server.services.workspace_snapshot import (
    reconcile_workspace_revisions,
    reconcile_workspace_storage,
)
from app.api_server.repositories.wecom_channels import WecomChannelRepository
from app.api_server.repositories.answer_images import AnswerImageRepository
from app.api_server.repositories.image_storage_profiles import (
    ImageStorageProfileRepository,
)
from app.api_server.services.answer_image_service import AnswerImageService
from app.api_server.services.storage_maintenance_lock import StorageMaintenanceLock
from app.api_server.storage.image_storage_profiles import (
    ImageStorageRegistry,
    build_image_storage_registry,
)


@dataclass
class ApiServices:
    knowledge_bases: KnowledgeBaseService
    documents: DocumentIngestionService
    document_previews: DocumentPreviewService
    fts: FTSIndexService
    vector: VectorIndexService
    applications: ApplicationService
    chat: ChatService
    models: ModelConfigService
    publishing: ApplicationReleaseService
    invocations: ApplicationInvocationService
    wecom: ApplicationWecomService | None = None
    images: AnswerImageService | None = None
    maintenance_lock: StorageMaintenanceLock | None = None
    close_storage: Callable[[], None] | None = None


def build_services(settings: ApiServerSettings) -> ApiServices:
    with ExitStack() as startup:
        maintenance_lock = StorageMaintenanceLock(settings.data_dir, exclusive=False)
        startup.callback(maintenance_lock.close)
        database_path = settings.database_path or settings.data_dir / "nianlun.sqlite3"
        factory = SQLiteConnectionFactory(
            database_path,
            timeout_seconds=settings.database_timeout_seconds,
            busy_timeout_ms=settings.database_busy_timeout_ms,
        )
        startup.callback(_drain_before_release, factory.dispose, maintenance_lock)
        initialize_database(factory)
        image_store = build_image_storage_registry(settings)
        startup.callback(_drain_before_release, image_store.close, maintenance_lock)
        ImageStorageProfileRepository(factory).bind_configured_profiles(
            image_store.locators
        )
        services = _assemble_services(
            settings, factory, image_store, startup, maintenance_lock
        )
        services.maintenance_lock = maintenance_lock

        def close_storage() -> None:
            factory.dispose()
            maintenance_lock.close()

        services.close_storage = close_storage
        startup.pop_all()
        return services


def _drain_before_release(
    action: Callable[[], None], lock: StorageMaintenanceLock
) -> None:
    try:
        action()
    except BaseException:
        lock.retain_until_exit()
        raise


def shutdown_services(services: ApiServices) -> None:
    """Drain every storage user before disposing connections and releasing lock."""
    lock = getattr(services, "maintenance_lock", None)
    with ExitStack() as shutdown:
        close_storage = getattr(services, "close_storage", None)
        if close_storage is not None:
            shutdown.callback(close_storage)
        elif lock is not None:
            shutdown.callback(lock.close)
        for name in ("images", "applications", "vector", "fts", "documents", "chat"):
            service = getattr(services, name, None)
            action = getattr(service, "shutdown", None)
            if action is not None:
                if lock is None:
                    shutdown.callback(action)
                else:
                    shutdown.callback(_drain_before_release, action, lock)
        invocations = getattr(services, "invocations", None)
        if invocations is not None:
            if lock is None:
                shutdown.callback(invocations.close)
            else:
                shutdown.callback(_drain_before_release, invocations.close, lock)


def _assemble_services(
    settings: ApiServerSettings,
    factory: SQLiteConnectionFactory,
    image_store: ImageStorageRegistry,
    startup: ExitStack,
    maintenance_lock: StorageMaintenanceLock,
) -> ApiServices:
    repository = SQLiteMetadataRepository(factory)
    chat_repository = SQLiteChatRepository(factory)
    chat_repository.fail_pending_messages(datetime.now(timezone.utc))
    models = ModelConfigService(repository)
    knowledge_bases = KnowledgeBaseService(
        repository,
        settings.workspace_root,
        models,
        fts_enabled=settings.fts_enabled,
    )
    fts = FTSIndexService(repository, knowledge_bases.require_record, settings)
    startup.callback(_drain_before_release, fts.shutdown, maintenance_lock)
    vector = VectorIndexService(
        repository,
        knowledge_bases.require_record,
        models.embedding_runtime_config,
        settings,
    )
    startup.callback(_drain_before_release, vector.shutdown, maintenance_lock)
    reconcile_workspace_revisions(
        repository,
        knowledge_bases,
        orphan_retention_seconds=settings.snapshot_min_retention_seconds,
    )
    reconcile_workspace_storage(
        repository,
        knowledge_bases,
        staging_retention_seconds=settings.staging_orphan_retention_seconds,
        artifact_retention_seconds=settings.snapshot_min_retention_seconds,
    )
    documents = DocumentIngestionService(
        repository,
        knowledge_bases,
        models,
        settings,
        fts_schedule=fts.schedule,
        vector_schedule=vector.schedule,
    )
    startup.callback(_drain_before_release, documents.shutdown, maintenance_lock)
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
    fts.index_access = applications.index_access
    vector.index_access = applications.index_access
    documents.recover_workspace_documents()
    knowledge_bases.reconcile()
    fts.recover_pending()
    vector.recover_pending()
    documents.recover()
    publishing = ApplicationReleaseService(PublishingRepository(factory), applications)
    image_repository = AnswerImageRepository(factory)
    images = AnswerImageService(
        image_repository,
        image_store,
        knowledge_bases.require_record,
        applications.require_record,
        profile_id=image_store.selected_profile_id,
        backend=image_store.selected_backend,
    )
    images.start_gc(grace_hours=settings.answer_image_orphan_grace_hours)
    startup.callback(images.shutdown)
    invocations = ApplicationInvocationService(
        publishing, applications, factory, settings, images
    )
    startup.callback(_drain_before_release, invocations.close, maintenance_lock)
    wecom = ApplicationWecomService(
        WecomChannelRepository(factory),
        publishing,
        applications,
        settings,
        GatewayControlClient(
            settings.gateway_control_urls,
            settings.gateway_control_token,
            settings.gateway_control_timeout_seconds,
        ),
    )
    return ApiServices(
        knowledge_bases=knowledge_bases,
        documents=documents,
        document_previews=DocumentPreviewService(repository, knowledge_bases),
        fts=fts,
        vector=vector,
        applications=applications,
        chat=ChatService(applications, chat_repository, images, settings),
        models=models,
        publishing=publishing,
        invocations=invocations,
        wecom=wecom,
        images=images,
    )


__all__ = ["ApiServices", "build_services", "shutdown_services"]
