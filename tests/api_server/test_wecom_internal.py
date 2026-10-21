from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_server.apis.internal.router import internal_router
from app.api_server.apis.v1.publishing_schemas import (
    ApplicationDeployRequest,
    WecomChannelCreateRequest,
    WecomChannelUpdateRequest,
)
from app.api_server.common.errors import ApiError, api_error_handler
from app.api_server.config import ApiServerSettings
from tests.api_server.test_application_publishing import publish
from tests.api_server.test_wecom_channel import api as api


@pytest.fixture
def channel(api):
    published = publish(api.releases, api.app_id)
    api.wecom.create(
        api.app_id,
        WecomChannelCreateRequest(
            type="wecom",
            bot_id="bot-internal",
            secret="internal-secret",
            default_response="fallback",
        ),
    )
    publication = api.wecom.deploy(
        api.app_id,
        ApplicationDeployRequest(
            channel_type="wecom",
            release_id=published.active_release.id,
            expected_deployment_version=0,
        ),
    )
    return SimpleNamespace(
        api=api,
        state=publication.wecom_channel,
        authorization={"Authorization": "Bearer " + "t" * 32},
    )


def test_internal_token_gates_snapshot_and_status():
    app = FastAPI()
    app.state.settings = ApiServerSettings()
    app.state.services = SimpleNamespace(wecom=None)
    app.include_router(internal_router)
    app.add_exception_handler(ApiError, api_error_handler)
    with TestClient(app) as client:
        response = client.get("/internal/v1/wecom/snapshot")
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"


def test_wrong_token_is_rejected(channel):
    response = channel.api.client.get(
        "/internal/v1/wecom/snapshot",
        headers={"Authorization": "Bearer " + "x" * 32},
    )
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


def test_snapshot_returns_enabled_channels(channel):
    response = channel.api.client.get(
        "/internal/v1/wecom/snapshot", headers=channel.authorization
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    assert len(payload["channels"]) == 1
    entry = payload["channels"][0]
    assert entry["channel_id"] == channel.state.id
    assert entry["revision"] == channel.state.config_revision
    assert entry["connection"]["bot_id"] == "bot-internal"
    assert entry["runtime"]["default_response"] == "fallback"


def test_status_reports_apply_revision_and_expiry_rules(channel):
    api = channel.api
    path = f"/internal/v1/wecom/channels/{channel.state.id}/status"
    stale = api.client.post(
        path,
        json={"revision": channel.state.config_revision - 1, "status": "online"},
        headers=channel.authorization,
    )
    assert stale.status_code == 204
    assert api.wecom.repository.by_channel_id(channel.state.id)["status"] == "offline"

    accepted = api.client.post(
        path,
        json={"revision": channel.state.config_revision, "status": "online"},
        headers=channel.authorization,
    )
    assert accepted.status_code == 204
    assert accepted.headers["Cache-Control"] == "no-store"
    reported = api.wecom.repository.by_channel_id(channel.state.id)
    assert reported["status"] == "online"
    assert reported["last_reported_at"] is not None

    expired = api.wecom.update(
        api.app_id,
        WecomChannelUpdateRequest(
            expected_config_revision=channel.state.config_revision,
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ),
    )
    ignored = api.client.post(
        path,
        json={"revision": expired.wecom_channel.config_revision, "status": "online"},
        headers=channel.authorization,
    )
    assert ignored.status_code == 204
    assert api.wecom.repository.by_channel_id(channel.state.id)["status"] == "offline"


def test_status_for_missing_channel_is_ignored(channel):
    response = channel.api.client.post(
        "/internal/v1/wecom/channels/missing/status",
        json={"revision": 1, "status": "offline"},
        headers=channel.authorization,
    )
    assert response.status_code == 204
