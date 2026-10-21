from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_server.apis.internal.router import internal_router
from app.api_server.apis.v1.apps.publishing import router
from app.api_server.apis.v1.publishing_schemas import WecomStatusRequest
from app.api_server.apis.v1.schemas import ApplicationCreateRequest
from app.api_server.common.errors import ApiError, api_error_handler
from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.database.models import ApplicationRelease, ApplicationWecomChannel
from app.api_server.database.models.publishing import ApplicationWecomBotRevision
from app.api_server.repositories.publishing import PublishingRepository
from app.api_server.repositories.sqlite_metadata import SQLiteMetadataRepository
from app.api_server.repositories.wecom_channels import WecomChannelRepository
from app.api_server.services.application_release_service import (
    ApplicationReleaseService,
)
from app.api_server.services.application_service import ApplicationService
from app.api_server.services.application_wecom_service import ApplicationWecomService
from app.api_server.services.model_config_service import ModelConfigService


@pytest.fixture
def store(tmp_path: Path):
    factory = SQLiteConnectionFactory(tmp_path / "wecom.sqlite3")
    initialize_database(factory)
    metadata = SQLiteMetadataRepository(factory)
    now = datetime.now(timezone.utc).isoformat()
    metadata.put(
        "knowledge_bases",
        "kb-1",
        {
            "id": "kb-1",
            "name": "Knowledge",
            "workspace_relpath": "kb-1",
            "created_at": now,
            "updated_at": now,
        },
    )
    for app_id in ("app-1", "app-2"):
        metadata.put(
            "applications",
            app_id,
            {
                "id": app_id,
                "name": app_id,
                "knowledge_base_id": "kb-1",
                "created_at": now,
                "updated_at": now,
            },
        )
    with factory.session_scope(write=True) as session:
        for sequence, release_id in enumerate(("release-1", "release-2"), start=1):
            session.add(
                ApplicationRelease(
                    id=release_id,
                    application_id="app-1",
                    sequence=sequence,
                    draft_config_version=1,
                    config_schema_version=1,
                    config_json="{}",
                    config_hash=f"hash-{sequence}",
                    created_at=now,
                )
            )
    yield factory, WecomChannelRepository(factory), PublishingRepository(factory)
    factory.dispose()


def _create(repository: WecomChannelRepository, *, bot_id="bot-1", expires_at=None):
    return repository.create(
        "app-1",
        bot_id=bot_id,
        secret="secret-1",
        default_response="fallback",
        expires_at=expires_at,
    )


def test_create_update_and_recreate_preserves_bot_revision(store):
    factory, repository, _ = store
    created = _create(repository, expires_at="2026-12-01T08:00:00+08:00")
    assert created["config_revision"] == 1
    assert created["deployment_version"] == 0
    assert created["enabled"] is False
    assert created["expires_at"] == "2026-12-01T00:00:00+00:00"
    assert repository.get("app-1")["id"] == created["id"]
    assert repository.by_channel_id(created["id"])["application_id"] == "app-1"
    assert repository.get("app-2") is None
    assert repository.by_channel_id("missing") is None

    updated = repository.update(
        "app-1",
        {"secret": "secret-2", "default_response": "new fallback"},
        expected_config_revision=1,
    )
    assert updated["config_revision"] == 2
    assert updated["secret"] == "secret-2"
    assert updated["default_response"] == "new fallback"
    assert updated["status"] == "offline"

    with pytest.raises(ApiError) as conflict:
        repository.update(
            "app-1", {"default_response": "stale"}, expected_config_revision=1
        )
    assert conflict.value.status_code == 409

    with factory.session_scope(write=True) as session:
        channel = session.get(ApplicationWecomChannel, created["id"])
        assert channel is not None
        session.delete(channel)

    recreated = _create(repository)
    assert recreated["config_revision"] == 3


def test_duplicate_bot_id_rejected(store):
    _, repository, _ = store
    _create(repository, bot_id="bot-shared")
    with pytest.raises(ApiError) as conflict:
        repository.create(
            "app-2",
            bot_id="bot-shared",
            secret="secret",
            default_response="fallback",
            expires_at=None,
        )
    assert conflict.value.status_code == 409

    with pytest.raises(ApiError):
        repository.create(
            "app-1",
            bot_id="bot-other",
            secret="secret",
            default_response="fallback",
            expires_at=None,
        )


@pytest.mark.parametrize(
    ("source_revision", "target_history", "expected_revision"),
    [(1, 0, 2), (3, 1, 4), (2, 1, 3), (2, 7, 8)],
)
def test_rebinding_fences_stale_updates_and_status(
    store, source_revision, target_history, expected_revision
):
    factory, repository, _ = store
    original = _create(repository)
    for revision in range(1, source_revision):
        original = repository.update(
            "app-1",
            {"default_response": f"revision {revision + 1}"},
            expected_config_revision=revision,
        )
    if target_history:
        with factory.session_scope(write=True) as session:
            session.add(
                ApplicationWecomBotRevision(
                    bot_id="bot-target",
                    last_revision=target_history,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )

    rebound = repository.update(
        "app-1",
        {"bot_id": "bot-target"},
        expected_config_revision=source_revision,
    )
    assert rebound["id"] == original["id"]
    assert rebound["config_revision"] == expected_revision
    with factory.session_scope() as session:
        counter = session.get(ApplicationWecomBotRevision, "bot-target")
        assert counter is not None
        assert counter.last_revision == expected_revision

    with pytest.raises(ApiError) as conflict:
        repository.update(
            "app-1",
            {"bot_id": "bot-1", "default_response": "stale"},
            expected_config_revision=source_revision,
        )
    assert conflict.value.status_code == 409
    assert (
        repository.report_status(
            original["id"], source_revision, "error", "AUTH_FAILED"
        )
        is False
    )
    assert repository.get("app-1")["status"] == "offline"
    assert (
        repository.report_status(rebound["id"], expected_revision, "online", None)
        is True
    )


def test_rebinding_back_and_recreating_preserves_allocated_revision(store):
    factory, repository, _ = store
    _create(repository)
    second = repository.update("app-1", {"bot_id": "bot-2"}, expected_config_revision=1)
    assert second["config_revision"] == 2
    first = repository.update("app-1", {"bot_id": "bot-1"}, expected_config_revision=2)
    assert first["config_revision"] == 3
    with factory.session_scope(write=True) as session:
        channel = session.get(ApplicationWecomChannel, first["id"])
        assert channel is not None
        session.delete(channel)
    recreated = _create(repository)
    assert recreated["config_revision"] == 4


def test_deploy_and_enable_allocate_revisions_and_channel_events(store):
    _, repository, publishing = store
    _create(repository)
    assert repository.last_deployed_at("app-1") is None

    with pytest.raises(ApiError):
        repository.set_enabled("app-1", True, expected_deployment_version=0)

    first = repository.deploy(
        "app-1",
        "release-1",
        expected_deployment_version=0,
        content_version=1,
    )
    assert first["enabled"] is True
    assert first["config_revision"] == 2
    assert first["deployment_version"] == 1
    assert first["active_release_id"] == "release-1"

    second = repository.deploy(
        "app-1",
        "release-2",
        expected_deployment_version=1,
        content_version=1,
    )
    assert second["config_revision"] == 2
    assert second["deployment_version"] == 2
    assert second["active_release_id"] == "release-2"

    disabled = repository.set_enabled("app-1", False, expected_deployment_version=2)
    assert disabled["enabled"] is False
    assert disabled["config_revision"] == 3
    assert disabled["deployment_version"] == 3

    enabled = repository.set_enabled("app-1", True, expected_deployment_version=3)
    assert enabled["enabled"] is True
    assert enabled["config_revision"] == 4
    assert enabled["deployment_version"] == 4

    events, total = publishing.deployment_events("app-1", 20, 0, channel_type="wecom")
    assert total == 4
    assert [item["action"] for item in events] == [
        "enable",
        "disable",
        "deploy",
        "deploy",
    ]
    assert all(item["channel_type"] == "wecom" for item in events)
    assert publishing.deployment_events("app-1", 20, 0) == ([], 0)
    assert isinstance(repository.last_deployed_at("app-1"), datetime)

    with pytest.raises(ApiError):
        repository.deploy(
            "app-1",
            "release-1",
            expected_deployment_version=0,
            content_version=1,
        )


def test_snapshot_only_includes_enabled_deployed_channels(store):
    _, repository, _ = store
    created = _create(repository, expires_at="2027-01-01T00:00:00+00:00")
    assert repository.snapshot() == []

    repository.deploy(
        "app-1",
        "release-1",
        expected_deployment_version=0,
        content_version=1,
    )
    snapshot = repository.snapshot()
    assert len(snapshot) == 1
    channel = snapshot[0]
    assert channel["channel_id"] == created["id"]
    assert channel["application_id"] == "app-1"
    assert channel["revision"] == 2
    assert channel["enabled"] is True
    assert channel["connection"] == {"bot_id": "bot-1", "secret": "secret-1"}
    assert channel["runtime"]["default_response"] == "fallback"
    assert channel["runtime"]["expires_at"] == "2027-01-01T00:00:00+00:00"

    repository.set_enabled("app-1", False, expected_deployment_version=1)
    assert repository.snapshot() == []


def test_report_status_enforces_revision_expiry_and_error_codes(store):
    _, repository, _ = store
    created = _create(repository)

    assert repository.report_status(created["id"], 99, "online", None) is False
    assert repository.report_status("missing", 1, "online", None) is False
    assert repository.report_status(created["id"], 1, "online", None) is True
    stored = repository.get("app-1")
    assert stored["status"] == "online"
    assert stored["error_code"] is None
    assert stored["last_reported_at"] is not None

    assert repository.report_status(created["id"], 1, "error", "AUTH_FAILED") is True
    stored = repository.get("app-1")
    assert stored["status"] == "error"
    assert stored["error_code"] == "AUTH_FAILED"

    assert repository.report_status(created["id"], 1, "online", None) is True
    assert repository.get("app-1")["error_code"] is None

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    repository.update("app-1", {"expires_at": past}, expected_config_revision=1)
    assert repository.report_status(created["id"], 2, "online", None) is False
    assert repository.report_status(created["id"], 2, "error", "NETWORK_ERROR") is False
    assert repository.report_status(created["id"], 2, "offline", None) is True


def test_expired_channel_rejects_deploy_and_enable(store):
    _, repository, _ = store
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    _create(repository, expires_at=future)
    repository.deploy(
        "app-1",
        "release-1",
        expected_deployment_version=0,
        content_version=1,
    )

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    repository.update("app-1", {"expires_at": past}, expected_config_revision=2)
    with pytest.raises(ApiError) as deploy_error:
        repository.deploy(
            "app-1",
            "release-2",
            expected_deployment_version=1,
            content_version=1,
        )
    assert deploy_error.value.status_code == 409

    repository.set_enabled("app-1", False, expected_deployment_version=1)
    with pytest.raises(ApiError) as enable_error:
        repository.set_enabled("app-1", True, expected_deployment_version=2)
    assert enable_error.value.status_code == 409


def test_update_can_clear_expiry_and_change_bot_id(store):
    _, repository, _ = store
    created = _create(repository, expires_at="2027-01-01T00:00:00+00:00")
    updated = repository.update(
        "app-1",
        {"expires_at": None, "default_response": "updated"},
        expected_config_revision=created["config_revision"],
    )
    assert updated["expires_at"] is None
    assert updated["secret"] == "secret-1"
    assert updated["config_revision"] == 2

    changed = repository.update(
        "app-1",
        {"bot_id": "bot-2"},
        expected_config_revision=2,
    )
    assert changed["bot_id"] == "bot-2"
    assert changed["config_revision"] == 3

    changed_again = repository.update(
        "app-1", {"bot_id": "bot-2"}, expected_config_revision=3
    )
    assert changed_again["config_revision"] == 4

    repository.create(
        "app-2",
        bot_id="bot-occupied",
        secret="secret",
        default_response="fallback",
        expires_at=None,
    )
    with pytest.raises(ApiError):
        repository.update(
            "app-1", {"bot_id": "bot-occupied"}, expected_config_revision=4
        )


class FakeGatewayControl:
    def __init__(self) -> None:
        self.refreshes = 0
        self.connects: list[tuple[str, int]] = []
        self.connect_result: dict[str, object] | None = {
            "channel_id": "unknown",
            "status": "online",
        }

    def refresh(self) -> None:
        self.refreshes += 1

    def connect(self, channel_id: str, revision: int) -> dict[str, object] | None:
        self.connects.append((channel_id, revision))
        return self.connect_result


@pytest.fixture
def api(tmp_path: Path):
    factory = SQLiteConnectionFactory(tmp_path / "wecom-api.sqlite3")
    initialize_database(factory)
    metadata = SQLiteMetadataRepository(factory)
    now = datetime.now(timezone.utc).isoformat()
    metadata.create_knowledge_base_with_revision(
        {
            "id": "kb",
            "name": "Knowledge",
            "created_at": now,
            "updated_at": now,
        },
        snapshot_relpath="snapshots/r0",
        manifest_sha256="a" * 64,
    )
    metadata.create_model_profile(
        {
            "id": "llm",
            "kind": "llm",
            "name": "Model",
            "model": "test-model",
            "base_url": "https://models.test/v1",
            "api_key": "model-credential",
            "context_window_tokens": 64_000,
        },
        now,
    )
    applications = ApplicationService(
        metadata,
        lambda _: {
            "id": "kb",
            "status": "ready",
            "snapshot_relpath": "snapshots/r1",
            "snapshot_manifest_sha256": "a" * 64,
            "content_version": 1,
        },
        model_config_service=ModelConfigService(metadata),
    )
    application = applications.create(
        ApplicationCreateRequest(name="App", knowledge_base_id="kb")
    )
    releases = ApplicationReleaseService(PublishingRepository(factory), applications)
    control = FakeGatewayControl()
    settings = ApiServerSettings(
        internal_token="t" * 32,
        gateway_control_urls=("http://gateway.test",),
        gateway_control_token="c" * 32,
    )
    wecom = ApplicationWecomService(
        WecomChannelRepository(factory), releases, applications, settings, control
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.services = SimpleNamespace(publishing=releases, wecom=wecom)
    app.include_router(router)
    app.include_router(internal_router)
    app.add_exception_handler(ApiError, api_error_handler)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            path=f"/api/v1/apps/{application.id}",
            app_id=application.id,
            factory=factory,
            applications=applications,
            releases=releases,
            wecom=wecom,
            control=control,
            settings=settings,
        )
    factory.dispose()


def _wecom_payload(**overrides):
    payload = {
        "type": "wecom",
        "bot_id": "bot-api",
        "secret": "super-secret-value",
        "default_response": "稍后再试",
    }
    payload.update(overrides)
    return payload


def test_management_api_requires_internal_token_and_hides_secret(api):
    without_token = ApplicationWecomService(
        WecomChannelRepository(api.factory),
        api.releases,
        api.applications,
        ApiServerSettings(),
        api.control,
    )
    api.app.state.services.wecom = without_token
    response = api.client.post(f"{api.path}/channels", json=_wecom_payload())
    assert response.status_code == 503

    api.app.state.services.wecom = api.wecom
    response = api.client.post(
        f"{api.path}/channels", json=_wecom_payload(default_response="  fallback  ")
    )
    assert response.status_code == 200
    data = response.json()["data"]
    state = data["wecom_channel"]
    assert state["bot_id"] == "bot-api"
    assert state["default_response"] == "fallback"
    assert state["has_secret"] is True
    assert state["config_revision"] == 1
    assert state["enabled"] is False
    assert data["api_channel_created"] is False
    assert "super-secret-value" not in response.text
    assert api.control.refreshes == 1

    assert (
        api.client.post(f"{api.path}/channels", json=_wecom_payload()).status_code
        == 409
    )


def test_update_semantics_and_revision_conflict(api):
    created = api.client.post(
        f"{api.path}/channels",
        json=_wecom_payload(expires_at="2027-01-01T00:00:00+08:00"),
    )
    assert created.status_code == 200
    assert datetime.fromisoformat(
        created.json()["data"]["wecom_channel"]["expires_at"]
    ).astimezone(timezone.utc) == datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc)

    updated = api.client.put(
        f"{api.path}/channels/wecom",
        json={"expected_config_revision": 1, "default_response": " updated "},
    )
    assert updated.status_code == 200
    state = updated.json()["data"]["wecom_channel"]
    assert state["default_response"] == "updated"
    assert state["config_revision"] == 2
    stored = WecomChannelRepository(api.factory).get(api.app_id)
    assert stored is not None and stored["secret"] == "super-secret-value"

    cleared = api.client.put(
        f"{api.path}/channels/wecom",
        json={"expected_config_revision": 2, "expires_at": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["data"]["wecom_channel"]["expires_at"] is None

    rotated = api.client.put(
        f"{api.path}/channels/wecom",
        json={"expected_config_revision": 3, "secret": "rotated-secret"},
    )
    assert rotated.status_code == 200
    assert rotated.json()["data"]["wecom_channel"]["config_revision"] == 4
    stored = WecomChannelRepository(api.factory).get(api.app_id)
    assert stored is not None and stored["secret"] == "rotated-secret"

    assert (
        api.client.put(
            f"{api.path}/channels/wecom",
            json={"expected_config_revision": 1, "default_response": "stale"},
        ).status_code
        == 409
    )
    assert (
        api.client.put(
            f"{api.path}/channels/wecom",
            json={"expected_config_revision": 4, "secret": None},
        ).status_code
        == 422
    )
    assert api.control.refreshes == 4


def test_deploy_keeps_channels_independent_and_filters_events(api):
    assert (
        api.client.post(f"{api.path}/channels", json={"type": "api"}).status_code == 200
    )
    published = api.client.post(
        f"{api.path}/publish",
        json={"expected_config_version": 1, "expected_deployment_version": 0},
    )
    assert published.status_code == 200
    api_state = published.json()["data"]
    release_id = api_state["active_release"]["id"]
    assert api_state["enabled"] is True
    assert api_state["deployment_version"] == 1

    created = api.client.post(f"{api.path}/channels", json=_wecom_payload())
    assert created.status_code == 200

    deployed = api.client.post(
        f"{api.path}/deployments",
        json={
            "channel_type": "wecom",
            "release_id": release_id,
            "expected_deployment_version": 0,
        },
    )
    assert deployed.status_code == 200
    data = deployed.json()["data"]
    wecom = data["wecom_channel"]
    assert wecom["enabled"] is True
    assert wecom["deployment_version"] == 1
    assert wecom["config_revision"] == 2
    assert wecom["active_release"]["id"] == release_id
    assert data["enabled"] is True
    assert data["deployment_version"] == 1
    assert data["active_release"]["id"] == release_id

    publication = api.client.get(f"{api.path}/publication").json()["data"]
    assert publication["wecom_channel"]["id"] == wecom["id"]
    assert publication["active_release"]["id"] == release_id

    api_events = api.client.get(f"{api.path}/deployments").json()["data"]
    assert api_events["total"] == 1
    assert api_events["items"][0]["channel_type"] == "api"
    wecom_events = api.client.get(f"{api.path}/deployments?channel_type=wecom").json()[
        "data"
    ]
    assert wecom_events["total"] == 1
    assert wecom_events["items"][0]["channel_type"] == "wecom"
    assert wecom_events["items"][0]["action"] == "deploy"

    disabled = api.client.patch(
        f"{api.path}/channels/wecom",
        json={"enabled": False, "expected_deployment_version": 1},
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["wecom_channel"]["enabled"] is False
    assert disabled.json()["data"]["enabled"] is True

    enabled = api.client.patch(
        f"{api.path}/channels/wecom",
        json={"enabled": True, "expected_deployment_version": 2},
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["wecom_channel"]["enabled"] is True
    assert enabled.json()["data"]["deployment_version"] == 1


def test_expired_channel_rejects_deploy_enable_and_connect(api):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    created = api.client.post(
        f"{api.path}/channels", json=_wecom_payload(expires_at=future)
    )
    assert created.status_code == 200
    release = api.client.post(
        f"{api.path}/releases", json={"expected_config_version": 1}
    )
    assert release.status_code == 200
    release_id = release.json()["data"]["id"]
    deployed = api.client.post(
        f"{api.path}/deployments",
        json={
            "channel_type": "wecom",
            "release_id": release_id,
            "expected_deployment_version": 0,
        },
    )
    assert deployed.status_code == 200

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    expired = api.client.put(
        f"{api.path}/channels/wecom",
        json={"expected_config_revision": 2, "expires_at": past},
    )
    assert expired.status_code == 200

    assert (
        api.client.post(
            f"{api.path}/deployments",
            json={
                "channel_type": "wecom",
                "release_id": release_id,
                "expected_deployment_version": 1,
            },
        ).status_code
        == 409
    )
    assert (
        api.client.patch(
            f"{api.path}/channels/wecom",
            json={"enabled": False, "expected_deployment_version": 1},
        ).status_code
        == 200
    )
    assert (
        api.client.patch(
            f"{api.path}/channels/wecom",
            json={"enabled": True, "expected_deployment_version": 2},
        ).status_code
        == 409
    )
    assert api.client.post(f"{api.path}/channels/wecom/connect").status_code == 409
    assert api.control.connects == []


def test_snapshot_status_and_connect_use_live_channel(api):
    created = api.client.post(f"{api.path}/channels", json=_wecom_payload())
    assert created.status_code == 200
    assert api.wecom.snapshot() == {"channels": []}

    release = api.client.post(
        f"{api.path}/releases", json={"expected_config_version": 1}
    ).json()["data"]
    deployed = api.client.post(
        f"{api.path}/deployments",
        json={
            "channel_type": "wecom",
            "release_id": release["id"],
            "expected_deployment_version": 0,
        },
    )
    assert deployed.status_code == 200
    state = deployed.json()["data"]["wecom_channel"]

    snapshot = api.wecom.snapshot()
    assert len(snapshot["channels"]) == 1
    channel = snapshot["channels"][0]
    assert channel["channel_id"] == state["id"]
    assert channel["revision"] == state["config_revision"]
    assert channel["connection"] == {
        "bot_id": "bot-api",
        "secret": "super-secret-value",
    }

    assert (
        api.wecom.report_status(
            state["id"],
            WecomStatusRequest(revision=state["config_revision"], status="online"),
        )
        is True
    )
    reported = WecomChannelRepository(api.factory).get(api.app_id)
    assert reported is not None and reported["status"] == "online"
    assert (
        api.wecom.report_status(
            state["id"],
            WecomStatusRequest(revision=state["config_revision"] - 1, status="online"),
        )
        is False
    )

    api.control.connect_result = {
        "channel_id": state["id"],
        "revision": state["config_revision"],
        "status": "online",
    }
    result = api.wecom.connect(api.app_id)
    assert result == api.control.connect_result
    assert api.control.connects == [(state["id"], state["config_revision"])]

    api.control.connect_result = None
    with pytest.raises(ApiError) as gateway_error:
        api.wecom.connect(api.app_id)
    assert gateway_error.value.status_code == 502


def test_settings_reject_tokens_with_surrounding_whitespace():
    # Internal routes compare digests of the raw header value, so a padded token
    # would authenticate nothing while looking configured.
    with pytest.raises(ValueError, match="NIANLUN_INTERNAL_TOKEN"):
        ApiServerSettings(internal_token=f" {'t' * 32} ")
    with pytest.raises(ValueError, match="NIANLUN_GATEWAY_CONTROL_TOKEN"):
        ApiServerSettings(
            gateway_control_urls=("http://gateway.test",),
            gateway_control_token=f" {'c' * 32} ",
        )
