from dataclasses import replace

import pytest
from sqlalchemy import select

from app.api_server.config import ApiServerSettings
from app.api_server.database.models import ImageStorageProfile
from app.api_server.services.container import build_services


def settings_for(tmp_path):
    return ApiServerSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        fts_enabled=False,
        vector_enabled=False,
    )


def stop(services):
    services.invocations.close()
    for name in ("chat", "documents", "fts", "vector", "images"):
        service = getattr(services, name)
        if service is not None:
            service.shutdown()


def test_default_store_is_registered_and_closed_after_worker_shutdown(tmp_path):
    services = build_services(settings_for(tmp_path))
    store = services.images.blob_store
    try:
        assert services.images.profile_id == "filesystem-default"
        assert services.images.backend == "filesystem"
        with services.images.repository.factory.session_scope() as session:
            assert session.get(ImageStorageProfile, "filesystem-default") is not None
        assert store._stores == {}
    finally:
        stop(services)
    assert store._closed


def test_switch_to_s3_preserves_local_profile_without_sdk_creation(
    tmp_path, monkeypatch
):
    import boto3

    monkeypatch.setattr(
        boto3, "client", lambda *a, **kw: pytest.fail("Unexpected SDK creation")
    )
    first_settings = settings_for(tmp_path)
    first = build_services(first_settings)
    stop(first)
    second_settings = replace(
        first_settings,
        answer_image_storage_backend="s3",
        answer_image_storage_profile_id="cloud",
        answer_image_storage_profiles={
            "filesystem-default": {
                "backend": "filesystem",
                "root": str(tmp_path / "data/answer-images"),
            },
            "cloud": {"backend": "s3", "bucket": "images", "region": "us-east-1"},
        },
    )
    second = build_services(second_settings)
    try:
        assert second.images.profile_id == "cloud"
        assert second.images.backend == "s3"
        with second.images.repository.factory.session_scope() as session:
            assert {
                row.profile_id for row in session.scalars(select(ImageStorageProfile))
            } == {"filesystem-default", "cloud"}
    finally:
        stop(second)


@pytest.mark.parametrize("change", ["missing", "relocated"])
def test_invalid_historical_configuration_rejected_before_recovery(
    tmp_path, monkeypatch, change
):
    import app.api_server.services.container as container

    initial = settings_for(tmp_path)
    stop(build_services(initial))
    stores = []
    original = container.build_image_storage_registry

    def capture(settings):
        registry = original(settings)
        stores.append(registry)
        return registry

    monkeypatch.setattr(container, "build_image_storage_registry", capture)
    monkeypatch.setattr(
        container.SQLiteChatRepository,
        "fail_pending_messages",
        lambda *a: pytest.fail("Recovery before validation"),
    )
    changed = replace(
        initial,
        answer_image_storage_profile_id="new"
        if change == "missing"
        else "filesystem-default",
        answer_image_storage_profiles={
            "new" if change == "missing" else "filesystem-default": {
                "backend": "filesystem",
                "root": str(tmp_path / "different"),
            },
        },
    )
    with pytest.raises(RuntimeError, match="Missing|changed"):
        build_services(changed)
    assert stores[0]._closed


def test_recovery_failure_closes_store_and_created_workers(tmp_path, monkeypatch):
    import app.api_server.services.container as container

    stores = []
    closed = []
    original = container.build_image_storage_registry

    def capture(settings):
        registry = original(settings)
        stores.append(registry)
        return registry

    def fail(*args):
        raise RuntimeError("Recovery failed")

    monkeypatch.setattr(container, "build_image_storage_registry", capture)
    monkeypatch.setattr(
        container.DocumentIngestionService, "recover_workspace_documents", fail
    )
    for name in ("DocumentIngestionService", "FTSIndexService", "VectorIndexService"):
        cls = getattr(container, name)
        original_shutdown = cls.shutdown

        def shutdown(self, original_shutdown=original_shutdown, name=name):
            closed.append(name)
            original_shutdown(self)

        monkeypatch.setattr(cls, "shutdown", shutdown)
    with pytest.raises(RuntimeError, match="Recovery failed"):
        build_services(settings_for(tmp_path))
    assert stores[0]._closed
    assert closed == [
        "DocumentIngestionService",
        "VectorIndexService",
        "FTSIndexService",
    ]
