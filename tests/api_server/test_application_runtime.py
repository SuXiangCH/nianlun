from __future__ import annotations

from types import SimpleNamespace

from app.api_server.config import ProviderConfig
from app.api_server.services.application_service import ApplicationService


class _ApplicationRepository:
    def __init__(self, application: dict) -> None:
        self.application = application

    def get(self, collection: str, item_id: str):
        assert collection == "applications"
        assert item_id == self.application["id"]
        return self.application


def test_runtime_cache_is_invalidated_when_vector_capability_changes(tmp_path):
    application = {
        "id": "app-1",
        "knowledge_base_id": "kb-1",
        "provider": "default",
        "model": "chat-model",
        "config_version": 1,
    }
    knowledge_base = {
        "workspace_dir": str(tmp_path),
        "content_version": 7,
        "fts_status": "ready",
        "fts_revision": 7,
        "fts_collection": "fts-v7",
        "vector_status": "pending",
        "vector_revision": None,
        "vector_collection": "vector-v7",
    }
    created = []

    def runtime_factory(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(model="chat-model", effective_url="test")

    service = ApplicationService(
        _ApplicationRepository(application),  # type: ignore[arg-type]
        lambda _knowledge_base_id: knowledge_base,
        runtime_factory=runtime_factory,
        provider_resolver=lambda _provider: ProviderConfig(model="chat-model"),
        vector_enabled=True,
    )

    first = service.runtime("app-1")
    assert service.runtime("app-1") is first
    assert len(created) == 1
    assert "knowledge_base_config" in created[0]
    assert "kb_config" not in created[0]

    knowledge_base["vector_status"] = "ready"
    knowledge_base["vector_revision"] = 7
    second = service.runtime("app-1")

    assert second is not first
    assert len(created) == 2


def test_degraded_vector_runtime_is_retried_instead_of_cached(tmp_path):
    application = {
        "id": "app-1",
        "knowledge_base_id": "kb-1",
        "provider": "default",
        "model": "chat-model",
        "config_version": 1,
    }
    knowledge_base = {
        "workspace_dir": str(tmp_path),
        "content_version": 7,
        "fts_status": "ready",
        "fts_revision": 7,
        "fts_collection": "fts-v7",
        "vector_status": "ready",
        "vector_revision": 7,
        "vector_collection": "vector-v7",
    }
    attempts = 0

    def runtime_factory(**_kwargs):
        nonlocal attempts
        attempts += 1
        return SimpleNamespace(
            model="chat-model",
            effective_url="test",
            kb=SimpleNamespace(has_vector=False),
        )

    service = ApplicationService(
        _ApplicationRepository(application),  # type: ignore[arg-type]
        lambda _knowledge_base_id: knowledge_base,
        runtime_factory=runtime_factory,
        provider_resolver=lambda _provider: ProviderConfig(model="chat-model"),
        vector_enabled=True,
    )

    first = service.runtime("app-1")
    second = service.runtime("app-1")
    assert second is not first
    assert attempts == 2
