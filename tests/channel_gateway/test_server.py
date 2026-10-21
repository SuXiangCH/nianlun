from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.channel_gateway.config import GatewaySettings
from app.channel_gateway.protocol import GatewayError
from app.channel_gateway.server import create_app

TOKEN = "c" * 32


def make_settings() -> GatewaySettings:
    return GatewaySettings.from_env(
        {
            "NIANLUN_INTERNAL_TOKEN": "i" * 32,
            "NIANLUN_GATEWAY_CONTROL_TOKEN": TOKEN,
        }
    )


class FakeManager:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.ready = True
        self.connect_result: dict[str, Any] = {
            "channel_id": "ch1",
            "revision": 3,
            "status": "online",
            "error_code": None,
        }
        self.connect_error: Exception | None = None
        self.connect_calls: list[tuple[str, int, str]] = []
        self.refresh_calls = 0
        self.refresh_result = True
        self.metrics = {"connections": 2, "messages_received": 3, "replies_ok": 1}

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def readiness(self) -> bool:
        return self.ready

    async def ensure_connected(
        self, channel_id: str, revision: int, request_id: str
    ) -> dict[str, Any]:
        self.connect_calls.append((channel_id, revision, request_id))
        if self.connect_error is not None:
            raise self.connect_error
        return dict(self.connect_result)

    def request_refresh(self) -> bool:
        self.refresh_calls += 1
        return self.refresh_result

    def metrics_snapshot(self) -> dict[str, int]:
        return dict(self.metrics)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_is_public_and_ready_reflects_manager():
    manager = FakeManager()
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

        manager.ready = False
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "NOT_READY"


def test_connect_requires_bearer_token():
    manager = FakeManager()
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        assert client.post("/api/channels/connect", json={}).status_code == 401
        assert (
            client.post(
                "/api/channels/connect",
                json={},
                headers=auth("wrong-token-that-is-long-enough"),
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/channels/connect",
                json={"channel_id": "ch1", "revision": 3, "request_id": "r1"},
                headers={"Authorization": TOKEN},
            ).status_code
            == 401
        )
        assert manager.connect_calls == []


def test_connect_passes_arguments_and_returns_result():
    manager = FakeManager()
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/channels/connect",
            json={"channel_id": "ch1", "revision": 3, "request_id": "r1"},
            headers=auth(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "channel_id": "ch1",
        "revision": 3,
        "status": "online",
        "error_code": None,
    }
    assert manager.connect_calls == [("ch1", 3, "r1")]


def test_connect_accepts_lowercase_bearer_scheme():
    manager = FakeManager()
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/channels/connect",
            json={"channel_id": "ch1", "revision": 3, "request_id": "r1"},
            headers={"Authorization": f"bearer {TOKEN}"},
        )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"channel_id": "ch1", "revision": 0, "request_id": "r1"},
        {"channel_id": "", "revision": 1, "request_id": "r1"},
        {"channel_id": "ch1", "revision": 1},
        {"channel_id": "ch1", "revision": 1, "request_id": "r1", "extra": True},
    ],
)
def test_connect_rejects_invalid_payload(payload):
    manager = FakeManager()
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        response = client.post("/api/channels/connect", json=payload, headers=auth())
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert manager.connect_calls == []


def test_connect_maps_gateway_error_status_and_code():
    manager = FakeManager()
    manager.connect_error = GatewayError("CONNECTION_CONFLICT", status_code=409)
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/channels/connect",
            json={"channel_id": "ch1", "revision": 3, "request_id": "r1"},
            headers=auth(),
        )
    assert response.status_code == 409
    assert response.json() == {
        "code": "CONNECTION_CONFLICT",
        "message": "CONNECTION_CONFLICT",
    }


def test_refresh_is_async_and_accepts_empty_body():
    manager = FakeManager()
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        assert (
            client.post("/api/channels/refresh", json={}, headers=auth()).status_code
            == 202
        )
        assert client.post("/api/channels/refresh", headers=auth()).status_code == 202
        assert manager.refresh_calls == 2

        response = client.post(
            "/api/channels/refresh", json={"extra": True}, headers=auth()
        )
        assert response.status_code == 400

        assert client.post("/api/channels/refresh", json={}).status_code == 401


def test_refresh_returns_503_when_gateway_stopped():
    manager = FakeManager()
    manager.refresh_result = False
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        response = client.post("/api/channels/refresh", json={}, headers=auth())
    assert response.status_code == 503
    assert response.json()["code"] == "GATEWAY_STOPPED"


def test_metrics_requires_token_and_renders_prometheus_names():
    manager = FakeManager()
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        assert client.get("/internal/metrics").status_code == 401
        response = client.get("/internal/metrics", headers=auth())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    lines = response.text.strip().splitlines()
    assert lines == [
        "channel_gateway_connections 2",
        "channel_gateway_messages_received 3",
        "channel_gateway_replies_ok 1",
    ]
    assert TOKEN not in response.text


def test_lifespan_starts_and_stops_manager():
    manager = FakeManager()
    app = create_app(manager, make_settings())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert manager.started == 1
        assert manager.stopped == 0
    assert manager.stopped == 1


def test_lifespan_can_skip_manager_lifecycle():
    manager = FakeManager()
    app = create_app(manager, make_settings(), start_manager=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert manager.started == 0
    assert manager.stopped == 0
