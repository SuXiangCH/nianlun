from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api_server.apis.v1.publishing_schemas import (
    ApiClientCreateRequest,
    ApiClientUpdateRequest,
    ApiKeyCreateRequest,
    ApplicationPublishRequest,
    ApplicationUpdateRequest,
    PublicationUpdateRequest,
)
from app.api_server.apis.v1.schemas import ApplicationCreateRequest, ModelProfileRequest
from app.api_server.common.errors import ApiError
from app.api_server.common.errors import api_error_handler
from app.api_server.apis.v1.apps.publishing import router
from app.api_server.database import SQLiteConnectionFactory
from app.api_server.database.models import Base
from app.api_server.database.models.publishing import (
    ApplicationApiKey,
    ApplicationRelease,
)
from app.api_server.repositories.publishing import PublishingRepository
from app.api_server.repositories.sqlite_metadata import SQLiteMetadataRepository
from app.api_server.services.application_release_service import (
    ApplicationReleaseService,
)
from app.api_server.services.application_service import ApplicationService
from app.api_server.services.model_config_service import ModelConfigService


@pytest.fixture
def publishing(tmp_path: Path):
    factory = SQLiteConnectionFactory(tmp_path / "metadata.sqlite3")
    Base.metadata.create_all(factory.engine)
    repository = SQLiteMetadataRepository(factory)
    now = datetime.now(timezone.utc).isoformat()
    repository.create_knowledge_base_with_revision(
        {
            "id": "kb",
            "name": "Knowledge",
            "created_at": now,
            "updated_at": now,
        },
        snapshot_relpath="snapshots/r0",
        manifest_sha256="a" * 64,
    )
    repository.create_model_profile(
        {
            "id": "llm",
            "kind": "llm",
            "name": "Model",
            "model": "test-model",
            "base_url": "https://models.test/v1",
            "api_key": "model-credential",
            "context_window_tokens": 64000,
        },
        now,
    )
    applications = ApplicationService(
        repository,
        lambda _: {
            "id": "kb",
            "status": "ready",
            "snapshot_relpath": "snapshots/r1",
            "snapshot_manifest_sha256": "a" * 64,
        },
        model_config_service=ModelConfigService(repository),
    )
    app = applications.create(
        ApplicationCreateRequest(name="App", knowledge_base_id="kb")
    )
    service = ApplicationReleaseService(PublishingRepository(factory), applications)
    yield service, app.id, repository
    factory.dispose()


def publish(service, app_id, *, config_version=1, deployment_version=0):
    return service.publish(
        app_id,
        ApplicationPublishRequest(
            expected_config_version=config_version,
            expected_deployment_version=deployment_version,
        ),
    )


def test_channel_creation_is_persistent_disabled_and_separate_from_publication(
    publishing,
):
    service, app_id, _ = publishing
    app = FastAPI()
    app.state.services = SimpleNamespace(publishing=service)
    app.include_router(router)
    app.add_exception_handler(ApiError, api_error_handler)
    with TestClient(app) as client:
        path = f"/api/v1/apps/{app_id}/channels"
        response = client.post(path, json={"type": "api"})
        assert response.status_code == 200
        state = response.json()["data"]
        assert state["api_channel_created"]
        assert not state["enabled"] and state["active_release"] is None
        assert state["deployment_version"] == 0
        duplicate = client.post(path, json={"type": "api"})
        assert duplicate.status_code == 409
        assert "已创建" in duplicate.json()["message"]
    reloaded = ApplicationReleaseService(service.repository, service.applications)
    assert reloaded.get_publication(app_id).api_channel_created
    assert reloaded.list_releases(app_id).total == 0
    assert not reloaded.list_clients(app_id)
    first = publish(reloaded, app_id)
    assert first.enabled and first.active_release is not None


@pytest.mark.parametrize("channel", ["wecom", "wechat", "unknown"])
def test_channel_creation_rejects_unsupported_types_without_writing(
    publishing, channel
):
    service, app_id, _ = publishing
    app = FastAPI()
    app.state.services = SimpleNamespace(publishing=service)
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/apps/{app_id}/channels", json={"type": channel}
        )
        assert response.status_code == 422
    assert service.repository.publication(app_id) == (None, None)


def test_channel_creation_requires_an_existing_agent_and_preserves_legacy_api(
    publishing,
):
    service, app_id, _ = publishing
    app = FastAPI()
    app.state.services = SimpleNamespace(publishing=service)
    app.include_router(router)
    app.add_exception_handler(ApiError, api_error_handler)
    with TestClient(app) as client:
        response = client.post("/api/v1/apps/missing/channels", json={"type": "api"})
        assert response.status_code == 404
    assert not service.get_publication(app_id).api_channel_created
    legacy = publish(service, app_id)
    assert legacy.api_channel_created
    assert legacy.enabled


def test_publish_is_immutable_idempotent_and_uses_explicit_effective_config(publishing):
    service, app_id, repository = publishing
    first = publish(service, app_id)
    assert first.enabled and first.deployment_version == 1
    assert first.active_release.config.temperature == 0
    assert first.active_release.config.thinking is None
    assert first.active_release.config.llm_model_id == "llm"
    assert first.active_release.config.context_window_tokens == 64000
    assert "credential" not in first.model_dump_json()
    same = publish(service, app_id, deployment_version=1)
    assert same.active_release.id == first.active_release.id
    assert same.deployment_version == 1
    changed = service.update(
        app_id,
        ApplicationUpdateRequest(
            expected_config_version=1,
            clarification_enabled=False,
        ),
    )
    assert changed.config_version == 2
    assert service.get_publication(app_id).has_unpublished_changes
    second = publish(service, app_id, config_version=2, deployment_version=1)
    assert second.active_release.sequence == 2
    assert service.require_release(
        app_id, first.active_release.id
    ).config.clarification_enabled
    assert service.list_releases(app_id).total == 2
    with repository.factory.session_scope() as session:
        assert len(session.scalars(select(ApplicationRelease)).all()) == 2


def test_stale_publish_cannot_enable_disabled_entry_and_restore_checks_profile(
    publishing,
):
    service, app_id, repository = publishing
    publish(service, app_id)
    disabled = service.set_enabled(
        app_id,
        PublicationUpdateRequest(
            enabled=False,
            expected_deployment_version=1,
        ),
    )
    assert disabled.deployment_version == 2
    with pytest.raises(ApiError) as error:
        publish(service, app_id, deployment_version=1)
    assert error.value.status_code == 409
    profile = repository.get_model_profile("llm")
    profile["base_url"] = "https://other.test/v1"
    repository.update_model_profile(profile, datetime.now(timezone.utc).isoformat())
    with pytest.raises(ApiError) as error:
        service.set_enabled(
            app_id,
            PublicationUpdateRequest(enabled=True, expected_deployment_version=2),
        )
    assert error.value.status_code == 409
    newer = publish(service, app_id, deployment_version=2)
    assert not newer.enabled and newer.active_release.sequence == 2


def test_key_write_only_rotation_scope_expiry_and_revocation(publishing):
    service, app_id, repository = publishing
    publish(service, app_id)
    client = service.create_client(app_id, ApiClientCreateRequest(name="Backend"))
    token = "nl_app_" + "a" * 32 + "." + "s" * 43
    key = service.register_key(
        app_id, client.id, ApiKeyCreateRequest(name="First", api_key=token)
    )
    assert token not in key.model_dump_json()
    assert "secret_digest" not in key.model_dump_json()
    assert service.authenticate(app_id, token).client_id == client.id
    with repository.factory.session_scope() as session:
        stored = session.get(ApplicationApiKey, key.key_id)
        assert stored.secret_digest != token
    with pytest.raises(ApiError) as error:
        service.authenticate("other-app", token)
    assert error.value.status_code == 404
    with pytest.raises(ApiError) as error:
        service.authenticate(app_id, token[:-1] + "x")
    assert error.value.status_code == 401
    with pytest.raises(ApiError) as error:
        service.authenticate("other-app", token[:-1] + "x")
    assert error.value.status_code == 401
    expired = "nl_app_" + "b" * 32 + "." + "t" * 43
    service.register_key(
        app_id,
        client.id,
        ApiKeyCreateRequest(
            name="Expired",
            api_key=expired,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
    )
    with pytest.raises(ApiError) as error:
        service.authenticate(app_id, expired)
    assert error.value.status_code == 401
    service.revoke_key(app_id, client.id, key.key_id)
    service.revoke_key(app_id, client.id, key.key_id)
    with pytest.raises(ApiError) as error:
        service.authenticate(app_id, token)
    assert error.value.status_code == 401
    assert len(service.list_keys(app_id, client.id)) == 2


def test_client_disable_and_application_metadata_do_not_mutate_release(publishing):
    service, app_id, _ = publishing
    first = publish(service, app_id)
    updated = service.update(
        app_id, ApplicationUpdateRequest(expected_config_version=1, name="Renamed")
    )
    assert updated.config_version == 1
    assert not service.get_publication(app_id).has_unpublished_changes
    client = service.create_client(app_id, ApiClientCreateRequest(name="Backend"))
    token = "nl_app_" + "c" * 32 + "." + "s" * 43
    service.register_key(
        app_id, client.id, ApiKeyCreateRequest(name="Key", api_key=token)
    )
    principal = service.authenticate(app_id, token)
    assert service.runtime_release(principal).id == first.active_release.id
    service.update_client(app_id, client.id, ApiClientUpdateRequest(enabled=False))
    with pytest.raises(ApiError) as error:
        service.runtime_release(principal)
    assert error.value.status_code == 403
    with pytest.raises(ApiError) as error:
        service.authenticate(app_id, token)
    assert error.value.status_code == 403


def test_publish_rechecks_profile_under_write_transaction(publishing, monkeypatch):
    service, app_id, repository = publishing
    original = service.repository.publish

    def change_profile_then_publish(*args, **kwargs):
        profile = repository.get_model_profile("llm")
        profile["model"] = "changed-model"
        repository.update_model_profile(profile, datetime.now(timezone.utc).isoformat())
        return original(*args, **kwargs)

    monkeypatch.setattr(service.repository, "publish", change_profile_then_publish)
    with pytest.raises(ApiError) as error:
        publish(service, app_id)
    assert error.value.status_code == 409
    assert service.repository.publication(app_id) == (None, None)


def test_model_credentials_can_rotate_without_changing_release(publishing):
    service, app_id, repository = publishing
    first = publish(service, app_id)
    profile = repository.get_model_profile("llm")
    profile["api_key"] = "rotated-model-credential"
    repository.update_model_profile(profile, datetime.now(timezone.utc).isoformat())
    service.validate_release(first.active_release.config)
    assert not service.get_publication(app_id).has_unpublished_changes


def test_publication_requires_model_credentials_without_exposing_them(publishing):
    service, app_id, repository = publishing
    profile = repository.get_model_profile("llm")
    profile["api_key"] = None
    repository.update_model_profile(profile, datetime.now(timezone.utc).isoformat())
    state = service.get_publication(app_id)
    assert not state.draft_ready
    with pytest.raises(ApiError) as error:
        publish(service, app_id)
    assert error.value.status_code == 503


def test_incompatible_release_is_rejected_explicitly(publishing):
    service, app_id, repository = publishing
    first = publish(service, app_id)
    with repository.factory.session_scope(write=True) as session:
        release = session.get(ApplicationRelease, first.active_release.id)
        release.config_json = release.config_json.replace(
            '"schema_version":1', '"schema_version":999'
        )
    with pytest.raises(ApiError) as error:
        service.require_release(app_id)
    assert error.value.status_code == 409


def test_publish_rechecks_committed_snapshot_in_transaction(publishing, monkeypatch):
    from app.api_server.database.models import KnowledgeBaseWorkspaceRevision

    service, app_id, repository = publishing
    original = service.repository.publish

    def remove_snapshot_then_publish(*args, **kwargs):
        with repository.factory.session_scope(write=True) as session:
            revision = session.scalar(select(KnowledgeBaseWorkspaceRevision))
            session.delete(revision)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.repository, "publish", remove_snapshot_then_publish)
    with pytest.raises(ApiError) as error:
        publish(service, app_id)
    assert error.value.status_code == 503
    assert service.repository.publication(app_id) == (None, None)


def test_management_routes_envelope_and_credential_response(publishing):
    service, app_id, _ = publishing
    app = FastAPI()
    app.state.services = SimpleNamespace(publishing=service)
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/apps/{app_id}/publish",
            json={
                "expected_config_version": 1,
                "expected_deployment_version": 0,
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["active_release"]["draft_config_version"] == 1
        created = client.post(
            f"/api/v1/apps/{app_id}/api-clients", json={"name": "Backend"}
        ).json()["data"]
        path = f"/api/v1/apps/{app_id}/api-clients/{created['id']}/keys"
        token = "nl_app_" + "d" * 32 + "." + "x" * 43
        registered = client.post(path, json={"name": "One", "api_key": token})
        assert registered.status_code == 200
        assert token not in registered.text
        assert "secret_digest" not in registered.text
        assert len(client.get(path).json()["data"]) == 1
        assert (
            client.post(path, json={"name": "Duplicate", "api_key": token}).status_code
            == 409
        )
        assert client.get(f"/api/v1/apps/{app_id}/releases?limit=0").status_code == 422


def test_application_delete_cascades_publication_and_credentials(publishing):
    from app.api_server.database.models.publishing import ApplicationDeployment

    service, app_id, repository = publishing
    publish(service, app_id)
    client = service.create_client(app_id, ApiClientCreateRequest(name="Backend"))
    token = "nl_app_" + "e" * 32 + "." + "x" * 43
    service.register_key(
        app_id, client.id, ApiKeyCreateRequest(name="Key", api_key=token)
    )
    with pytest.raises(ApiError) as error:
        service.applications.delete(app_id)
    assert error.value.status_code == 409
    service.set_enabled(
        app_id, PublicationUpdateRequest(enabled=False, expected_deployment_version=1)
    )
    service.applications.delete(app_id)
    with repository.factory.session_scope() as session:
        assert session.scalar(select(ApplicationRelease)) is None
        assert session.scalar(select(ApplicationDeployment)) is None
        assert session.scalar(select(ApplicationApiKey)) is None


def test_draft_update_rechecks_selected_model_existence(publishing, monkeypatch):
    service, app_id, repository = publishing
    now = datetime.now(timezone.utc).isoformat()
    repository.create_model_profile(
        {
            "id": "alternate",
            "kind": "llm",
            "name": "Alternate",
            "model": "other",
            "base_url": "https://other.test/v1",
            "api_key": None,
        },
        now,
    )
    original = service.repository.update_application

    def remove_profile_then_update(*args, **kwargs):
        repository.delete_model_profile("alternate")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        service.repository, "update_application", remove_profile_then_update
    )
    with pytest.raises(ApiError) as error:
        service.update(
            app_id,
            ApplicationUpdateRequest(
                expected_config_version=1,
                llm_model_id="alternate",
            ),
        )
    assert error.value.status_code == 409
    assert service.applications.require_record(app_id)["model"] == "llm"


def _public_conversation(publishing, *, expired=False):
    from app.api_server.database.models import Conversation

    service, app_id, repository = publishing
    release = service.require_release(app_id)
    now = datetime.now(timezone.utc)
    with repository.factory.session_scope(write=True) as session:
        session.add(
            Conversation(
                id="api-conversation",
                application_id=app_id,
                title="",
                status="active",
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
                channel="api",
                api_client_id="client",
                external_user_id="user",
                release_id=release.id,
                expires_at=(now + timedelta(days=-1 if expired else 1)).isoformat(),
            )
        )
    return release


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_url", "https://different.test/v1"),
        ("model", "different"),
        ("context_window_tokens", 32000),
    ],
)
def test_live_release_blocks_model_execution_changes_but_allows_credentials(
    publishing, field, value
):
    service, app_id, repository = publishing
    publish(service, app_id)
    models = service.applications.model_config_service
    request = {
        "kind": "llm",
        "name": "Renamed",
        "model": "test-model",
        "base_url": "https://models.test/v1",
        "context_window_tokens": 64000,
        "api_key": "rotated",
        "is_default": True,
    }
    models.update_profile("llm", ModelProfileRequest(**request))
    assert repository.get_model_profile("llm")["api_key"] == "rotated"
    request[field] = value
    with pytest.raises(ApiError) as error:
        models.update_profile("llm", ModelProfileRequest(**request))
    assert error.value.status_code == 409
    assert repository.get_model_profile("llm")[field] != value


def test_unexpired_conversation_protects_profile_and_application_until_expiry(
    publishing,
):
    from app.api_server.database.models import Conversation

    service, app_id, repository = publishing
    publish(service, app_id)
    _public_conversation(publishing)
    service.set_enabled(
        app_id, PublicationUpdateRequest(enabled=False, expected_deployment_version=1)
    )
    profile = repository.get_model_profile("llm")
    profile["model"] = "different"
    with pytest.raises(ApiError) as error:
        repository.update_model_profile(profile, datetime.now(timezone.utc).isoformat())
    assert error.value.status_code == 409
    with pytest.raises(ApiError) as error:
        service.applications.delete(app_id)
    assert error.value.status_code == 409
    with repository.factory.session_scope(write=True) as session:
        session.get(Conversation, "api-conversation").expires_at = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()
    repository.update_model_profile(profile, datetime.now(timezone.utc).isoformat())
    service.applications.delete(app_id)


def test_active_run_protects_expired_conversation_application(publishing):
    from app.api_server.database.models.application_runs import ApplicationRun

    service, app_id, repository = publishing
    publish(service, app_id)
    release = _public_conversation(publishing, expired=True)
    service.set_enabled(
        app_id, PublicationUpdateRequest(enabled=False, expected_deployment_version=1)
    )
    now = datetime.now(timezone.utc).isoformat()
    with repository.factory.session_scope(write=True) as session:
        session.add(
            ApplicationRun(
                id="run",
                application_id=app_id,
                client_id="client",
                user_id="user",
                conversation_id="api-conversation",
                release_id=release.id,
                message_id="message",
                idempotency_key="retry",
                request_fingerprint="hash",
                status="running",
                knowledge_base_content_version=0,
                created_at=now,
                updated_at=now,
            )
        )
    with pytest.raises(ApiError) as error:
        service.applications.delete(app_id)
    assert error.value.status_code == 409


def test_old_published_knowledge_base_cannot_be_deleted_after_draft_changes(
    publishing, tmp_path
):
    from app.api_server.services.knowledge_base_service import KnowledgeBaseService

    service, app_id, repository = publishing
    publish(service, app_id)
    now = datetime.now(timezone.utc).isoformat()
    repository.create_knowledge_base_with_revision(
        {
            "id": "other-kb",
            "name": "Other",
            "created_at": now,
            "updated_at": now,
        },
        snapshot_relpath="snapshots/r0",
        manifest_sha256="b" * 64,
    )
    service.update(
        app_id,
        ApplicationUpdateRequest(
            expected_config_version=1, knowledge_base_id="other-kb"
        ),
    )
    knowledge = KnowledgeBaseService(
        repository,
        tmp_path / "workspaces",
        service.applications.model_config_service,
        fts_enabled=False,
    )
    workspace = tmp_path / "workspaces" / "kb"
    workspace.mkdir()
    with pytest.raises(ApiError) as error:
        knowledge.delete("kb")
    assert error.value.status_code == 409
    assert workspace.exists()
    service.set_enabled(
        app_id, PublicationUpdateRequest(enabled=False, expected_deployment_version=1)
    )
    knowledge.delete("kb")
    assert not workspace.exists()


def test_profile_delete_retains_live_old_release_after_draft_moves(publishing):
    service, app_id, repository = publishing
    publish(service, app_id)
    now = datetime.now(timezone.utc).isoformat()
    repository.create_model_profile(
        {
            "id": "alternate",
            "kind": "llm",
            "name": "Alternate",
            "model": "other",
            "base_url": "https://other.test/v1",
            "is_default": True,
        },
        now,
    )
    service.update(
        app_id,
        ApplicationUpdateRequest(expected_config_version=1, llm_model_id="alternate"),
    )
    models = service.applications.model_config_service
    with pytest.raises(ApiError) as error:
        models.delete_profile("llm")
    assert error.value.status_code == 409
    assert repository.get_model_profile("llm") is not None
    service.set_enabled(
        app_id, PublicationUpdateRequest(enabled=False, expected_deployment_version=1)
    )
    models.delete_profile("llm")
    assert repository.get_model_profile("llm") is None


def test_expired_terminal_run_allows_application_cleanup(publishing):
    from app.api_server.database.models.application_runs import ApplicationRun

    service, app_id, repository = publishing
    publish(service, app_id)
    release = _public_conversation(publishing, expired=True)
    service.set_enabled(
        app_id, PublicationUpdateRequest(enabled=False, expected_deployment_version=1)
    )
    now = datetime.now(timezone.utc).isoformat()
    with repository.factory.session_scope(write=True) as session:
        session.add(
            ApplicationRun(
                id="run",
                application_id=app_id,
                client_id="client",
                user_id="user",
                conversation_id="api-conversation",
                release_id=release.id,
                message_id="message",
                idempotency_key="retry",
                request_fingerprint="hash",
                status="completed",
                knowledge_base_content_version=0,
                created_at=now,
                updated_at=now,
                finished_at=now,
            )
        )
    service.applications.delete(app_id)
    with repository.factory.session_scope() as session:
        assert session.scalar(select(ApplicationRun)) is None


def test_incompatible_active_release_does_not_block_valid_draft(
    publishing, monkeypatch
):
    service, app_id, _ = publishing
    publish(service, app_id)
    monkeypatch.setattr(
        "app.api_server.services.application_release_service.PROMPT_VERSION", 999
    )
    state = service.get_publication(app_id)
    assert not state.dependencies_ready
    assert state.draft_ready
    assert state.draft_error is None
    refreshed = publish(service, app_id, deployment_version=1)
    assert refreshed.dependencies_ready
    assert refreshed.active_release.sequence == 2


def test_knowledge_base_delete_rechecks_references_before_filesystem_changes(
    publishing, tmp_path, monkeypatch
):
    from app.api_server.services.knowledge_base_service import KnowledgeBaseService

    service, _, repository = publishing
    knowledge = KnowledgeBaseService(
        repository,
        tmp_path / "workspaces",
        service.applications.model_config_service,
        fts_enabled=False,
    )
    workspace = tmp_path / "workspaces" / "kb"
    workspace.mkdir()
    marker = workspace / "source.md"
    marker.write_text("retained content")
    # The write transaction itself must recheck references, so no stale-read stub.
    with pytest.raises(ApiError, match="应用绑定"):
        knowledge.delete("kb")
    assert marker.read_text() == "retained content"
    assert repository.get("knowledge_bases", "kb") is not None


def test_release_runtime_maps_unavailable_retrieval_to_503(
    publishing, tmp_path, monkeypatch
):
    from nianlun.knowledgebase.factory import KnowledgeBaseFactory

    service, app_id, _ = publishing
    publish(service, app_id)
    config = service.require_release(app_id).config.model_copy(
        update={"llm_model_id": "llm"}
    )

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("知识库当前没有可用于本地检索的 committed snapshot")

    monkeypatch.setattr(
        service.applications,
        "knowledge_base_lookup",
        lambda _id: {
            "id": "kb",
            "workspace_dir": str(tmp_path),
            "content_version": 1,
            "snapshot_relpath": "snapshots/r1",
            "snapshot_manifest_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(KnowledgeBaseFactory, "create", unavailable)
    with pytest.raises(ApiError) as error:
        service.applications.release_runtime(
            config, None, {"workspace_dir": str(tmp_path)}
        )
    assert error.value.status_code == 503


def test_console_runtime_maps_unavailable_retrieval_to_503(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    knowledge = {
        "id": "kb",
        "workspace_dir": str(tmp_path),
        "content_version": 1,
        "snapshot_relpath": "snapshots/r1",
        "snapshot_manifest_sha256": "a" * 64,
    }
    item = {
        "id": "app-1",
        "knowledge_base_id": "kb",
        "model": "console-model",
        "provider": "default",
        "agent_version": "v0",
        "created_at": now,
        "updated_at": now,
    }

    def runtime_factory(**_kwargs):
        raise RuntimeError("知识库当前没有可用于本地检索的 committed snapshot")

    service = ApplicationService(
        SimpleNamespace(get=lambda _table, _id: dict(item)),
        lambda _id: dict(knowledge),
        runtime_factory=runtime_factory,
    )
    with pytest.raises(ApiError) as error:
        service.runtime("app-1")
    assert error.value.status_code == 503
