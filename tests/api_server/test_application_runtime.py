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
    assert created[0]["agent_version"] == "v0"

    knowledge_base["vector_status"] = "ready"
    knowledge_base["vector_revision"] = 7
    second = service.runtime("app-1")

    assert second is not first
    assert len(created) == 2


def test_runtime_cache_isolates_bundles_and_image_capability(tmp_path):
    from nianlun.agent.prompt_registry import (
        CURRENT_PROMPT_BUNDLE,
        LEGACY_PROMPT_BUNDLE,
    )

    application = {
        "id": "app-1",
        "knowledge_base_id": "kb-1",
        "provider": "default",
        "model": "model",
    }
    service = ApplicationService(
        _ApplicationRepository(application),
        lambda _: {"workspace_dir": str(tmp_path)},
        runtime_factory=lambda **kw: SimpleNamespace(**kw),
    )
    old = service.runtime("app-1", prompt_bundle=LEGACY_PROMPT_BUNDLE)
    current_text = service.runtime("app-1", prompt_bundle=CURRENT_PROMPT_BUNDLE)
    current_images = service.runtime(
        "app-1", prompt_bundle=CURRENT_PROMPT_BUNDLE, image_capability=True
    )
    assert old is not current_text and current_text is not current_images
    assert service.runtime("app-1", prompt_bundle=LEGACY_PROMPT_BUNDLE) is old
    assert service.runtime("app-1", prompt_bundle=CURRENT_PROMPT_BUNDLE) is current_text
    assert (
        service.runtime(
            "app-1", prompt_bundle=CURRENT_PROMPT_BUNDLE, image_capability=True
        )
        is current_images
    )
    from dataclasses import replace
    from nianlun.agent.contracts import AnswerImagePolicy

    smaller = replace(AnswerImagePolicy(), max_answer_images=1)
    limited = service.runtime(
        "app-1",
        prompt_bundle=CURRENT_PROMPT_BUNDLE,
        image_capability=True,
        image_policy=smaller,
    )
    assert limited is not current_images
    assert limited.image_policy is smaller
    assert current_images.image_policy.max_answer_images == 32
    service.invalidate_runtime("app-1")
    assert service.runtime("app-1", prompt_bundle=LEGACY_PROMPT_BUNDLE) is not old


def test_runtime_factory_receives_persisted_agent_version(tmp_path):
    application = {
        "id": "app-1",
        "knowledge_base_id": "kb-1",
        "provider": "default",
        "model": "chat-model",
        "config_version": 1,
        "agent_version": "v1",
    }
    knowledge_base = {
        "workspace_dir": str(tmp_path),
        "content_version": 1,
        "fts_status": "ready",
        "fts_revision": 1,
        "fts_collection": "fts-v1",
        "vector_status": "disabled",
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
    )

    service.runtime("app-1")

    assert created[0]["agent_version"] == "v1"


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


def test_shutdown_closes_every_cached_reader_even_when_one_close_fails(tmp_path):
    import pytest
    from nianlun.agent.prompt_registry import CURRENT_PROMPT_BUNDLE

    closed = []

    def factory(**kwargs):
        index = len(readers)

        def close():
            closed.append(index)
            if index == 0:
                raise OSError("close failed")

        runtime = SimpleNamespace(kb=SimpleNamespace(close=close), **kwargs)
        readers.append(runtime)
        return runtime

    readers = []
    service = ApplicationService(
        _ApplicationRepository(
            {
                "id": "app-1",
                "knowledge_base_id": "kb",
                "provider": "default",
                "model": "model",
            }
        ),
        lambda _: {"workspace_dir": str(tmp_path)},
        runtime_factory=factory,
    )
    service.runtime("app-1")
    service.runtime("app-1", prompt_bundle=CURRENT_PROMPT_BUNDLE)
    with pytest.raises(OSError, match="close failed"):
        service.shutdown()
    assert set(closed) == {0, 1}
    service.shutdown()
    assert len(closed) == 2
