from pathlib import Path
import base64
import json
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.main import create_app
from app.api_server.repositories import SQLiteMetadataRepository
import app.api_server.services.model_config_service as model_module
import nianlun.models.llm as llm_module


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(
        create_app(
            ApiServerSettings(
                data_dir=tmp_path / "api",
                workspace_root=tmp_path / "workspaces",
                fts_enabled=False,
            )
        )
    ) as client:
        yield client


def _config(**overrides):
    return {
        "kind": "llm",
        "name": "vision-model",
        "model": "vision-model",
        "base_url": "https://model.test/v1",
        **overrides,
    }


@pytest.mark.parametrize("supports_vision", [None, True, False])
def test_model_vision_roundtrip_and_explicit_reset(client, supports_vision):
    response = client.post(
        "/api/v1/models", json=_config(supports_vision=supports_vision)
    )
    assert response.status_code == 200
    profile = response.json()["data"]
    assert profile["supports_vision"] is supports_vision
    path = f"/api/v1/models/{profile['id']}"
    assert client.get(path).json()["data"]["supports_vision"] is supports_vision
    assert (
        client.get("/api/v1/models").json()["data"][0]["supports_vision"]
        is supports_vision
    )
    # Older clients omitting the new field must not clear an explicit choice.
    response = client.put(path, json=_config(name="renamed-model"))
    assert response.status_code == 200
    assert response.json()["data"]["supports_vision"] is supports_vision
    response = client.put(path, json=_config(supports_vision=None))
    assert response.status_code == 200
    assert response.json()["data"]["supports_vision"] is None


def test_model_vision_defaults_to_unknown(client):
    profile = client.post("/api/v1/models", json=_config()).json()["data"]
    assert profile["supports_vision"] is None
    path = f"/api/v1/models/{profile['id']}"
    for value in (True, False, None):
        response = client.put(path, json=_config(supports_vision=value))
        assert response.status_code == 200
        assert response.json()["data"]["supports_vision"] is value


@pytest.fixture
def probe_llm(monkeypatch):
    calls = []
    settings = {}

    def invoke(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "OK" if isinstance(prompt, str) else "图片中有一只猫"

    def build(**kwargs):
        settings.update(kwargs)
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(model_module, "build_chat_model", build)
    return calls, settings


@pytest.mark.parametrize("supports_vision", [True, False, None])
def test_saved_model_probes_images_only_when_vision_is_enabled(
    client, probe_llm, supports_vision
):
    calls, settings = probe_llm
    profile = client.post(
        "/api/v1/models", json=_config(supports_vision=supports_vision)
    ).json()["data"]
    path = f"/api/v1/models/{profile['id']}"
    response = client.post(f"{path}/test")
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["ok"] is True
    assert result["checks"][0]["capability"] == "text"
    assert result["checks"][0]["status"] == "passed"
    assert len(calls) == (2 if supports_vision else 1)
    if supports_vision:
        assert result["checks"][1]["capability"] == "vision"
        assert result["checks"][1]["status"] == "passed"
        messages, _kwargs = calls[1]
        sample = messages[0].content[1]["image_url"]["url"]
        assert base64.b64decode(sample.split(",", 1)[1]).startswith(
            b"\x89PNG\r\n\x1a\n"
        )
        assert result["checks"][1]["message"] == "图片输入请求成功"
    assert settings["request_timeout_seconds"] <= 30
    assert settings["max_retries"] == 0
    assert client.get(path).json()["data"]["supports_vision"] is supports_vision


@pytest.mark.parametrize("token_limit_field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("image_status", [200, 400])
def test_probe_real_sdk_supports_strict_endpoint_token_limit_variants(
    client, monkeypatch, token_limit_field, image_status
):
    payloads = []

    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        payloads.append(payload)
        allowed = {"model", "messages", "stream", "temperature", token_limit_field}
        unsupported = sorted(set(payload) - allowed)
        if unsupported:
            return httpx.Response(
                400,
                json={"error": {"message": f"Unsupported parameters: {unsupported}"}},
            )
        if isinstance(payload["messages"][0]["content"], list) and image_status != 200:
            return httpx.Response(
                image_status, json={"error": {"message": "Image input rejected"}}
            )
        return httpx.Response(
            200,
            json={
                "id": "probe-response",
                "object": "chat.completion",
                "created": 0,
                "model": "vision-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    real_chat_model = llm_module.ChatOpenAI
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        monkeypatch.setattr(
            llm_module,
            "ChatOpenAI",
            lambda **kwargs: real_chat_model(http_client=http_client, **kwargs),
        )
        profile = client.post(
            "/api/v1/models", json=_config(supports_vision=True, api_key="test-key")
        ).json()["data"]
        path = f"/api/v1/models/{profile['id']}"
        response = client.post(f"{path}/test")

    assert response.status_code == 200
    result = response.json()["data"]
    assert result["ok"] is (image_status == 200)
    assert [check["status"] for check in result["checks"]] == [
        "passed",
        "passed" if image_status == 200 else "failed",
    ]
    assert len(payloads) == 2
    image = payloads[1]["messages"][0]["content"][1]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    assert client.get(path).json()["data"]["supports_vision"] is True


@pytest.mark.parametrize("override", [True, False, None])
def test_unsaved_vision_override_is_used_without_updating_the_profile(
    client, probe_llm, override
):
    calls, _ = probe_llm
    profile = client.post("/api/v1/models", json=_config(supports_vision=True)).json()[
        "data"
    ]
    path = f"/api/v1/models/{profile['id']}"
    result = client.post(
        f"{path}/test", json={"target": "llm", "supports_vision": override}
    ).json()["data"]
    assert result["ok"] is True
    assert len(calls) == (2 if override else 1)
    assert client.get(path).json()["data"]["supports_vision"] is True


def test_new_draft_can_probe_vision_without_saving(client, probe_llm):
    calls, _ = probe_llm
    result = client.post(
        "/api/v1/models/test",
        json={
            "target": "llm",
            "model": "draft",
            "base_url": "https://model.test/v1",
            "supports_vision": True,
        },
    ).json()["data"]
    assert result["ok"] is True
    assert len(calls) == 2
    assert client.get("/api/v1/models").json()["data"] == []


@pytest.mark.parametrize("reply", ["OK", "图片中有一只猫", '["red"]', ""])
def test_image_input_probe_does_not_grade_the_model_reply(client, monkeypatch, reply):
    monkeypatch.setattr(
        model_module,
        "build_chat_model",
        lambda **_kwargs: SimpleNamespace(invoke=lambda *_args, **_options: reply),
    )
    result = client.post(
        "/api/v1/models/test",
        json={
            "target": "llm",
            "model": "draft",
            "base_url": "https://model.test/v1",
            "supports_vision": True,
        },
    ).json()["data"]
    assert result["ok"] is True
    assert [check["status"] for check in result["checks"]] == ["passed", "passed"]


@pytest.mark.parametrize(
    "failure", ["text", "vision", "vision_truncated", "malformed_api"]
)
def test_probe_failure_keeps_manual_vision_and_returns_separate_redacted_results(
    client, monkeypatch, failure
):
    calls = []
    secret = "private-test-credential"

    def invoke(prompt, **_kwargs):
        calls.append(prompt)
        if isinstance(prompt, str):
            if failure == "text":
                raise RuntimeError(f"unauthorized: {secret}")
            return "OK"
        if failure == "malformed_api":
            raise json.JSONDecodeError("invalid endpoint response", "", 0)
        image = prompt[0].content[1]["image_url"]["url"]
        if failure == "vision_truncated":
            image = image[:100] + "..."
        raise RuntimeError(f"image rejected: {secret} {image}")

    monkeypatch.setattr(
        model_module,
        "build_chat_model",
        lambda **_kwargs: SimpleNamespace(invoke=invoke),
    )
    profile = client.post(
        "/api/v1/models", json=_config(supports_vision=True, api_key=secret)
    ).json()["data"]
    path = f"/api/v1/models/{profile['id']}"
    response = client.post(f"{path}/test")
    result = response.json()["data"]
    assert result["ok"] is False
    assert [check["status"] for check in result["checks"]] == (
        ["failed", "skipped"] if failure == "text" else ["passed", "failed"]
    )
    assert secret not in response.text
    assert "data:image" not in response.text
    assert client.get(path).json()["data"]["supports_vision"] is True
    assert len(calls) == (1 if failure == "text" else 2)
    if failure == "malformed_api":
        assert (
            result["checks"][1]["message"]
            == "图片请求失败：接口响应格式无效，请检查接口兼容性"
        )


@pytest.mark.parametrize("value", ["true", "false", 1, 0, "unknown"])
def test_model_vision_rejects_non_boolean_values(client, value):
    response = client.post("/api/v1/models", json=_config(supports_vision=value))
    assert response.status_code == 422
    assert client.get("/api/v1/models").json()["data"] == []


@pytest.mark.parametrize("kind", ["embedding", "parser"])
@pytest.mark.parametrize("value", [True, False])
def test_model_vision_is_only_available_for_llm(client, kind, value):
    response = client.post(
        "/api/v1/models", json=_config(kind=kind, supports_vision=value)
    )
    assert response.status_code == 422


def test_vision_migration_preserves_old_profiles_and_is_repeatable(tmp_path: Path):
    factory = SQLiteConnectionFactory(tmp_path / "legacy.sqlite3")
    connection = factory.connect()
    try:
        connection.executescript(
            """
            CREATE TABLE model_profiles (
                id VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL,
                name VARCHAR NOT NULL, base_url VARCHAR, api_key TEXT,
                is_default BOOLEAN NOT NULL, created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL
            );
            CREATE TABLE llm_model_profiles (
                profile_id VARCHAR PRIMARY KEY REFERENCES model_profiles(id),
                model VARCHAR, context_window_tokens INTEGER
            );
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            INSERT INTO model_profiles VALUES (
                'old', 'llm', 'existing-model', 'https://model.test/v1', NULL,
                1, '2026-10-19T00:00:00Z', '2026-10-19T00:00:00Z'
            );
            INSERT INTO llm_model_profiles VALUES ('old', 'existing-model', 32000);
            INSERT INTO schema_migrations VALUES (11, 'previous', '2026-10-19');
            """
        )
    finally:
        connection.close()
    initialize_database(factory)
    repository = SQLiteMetadataRepository(factory)
    profile = repository.get_model_profile("old")
    assert profile["supports_vision"] is None
    assert profile["model"] == "existing-model"
    assert profile["context_window_tokens"] == 32000
    profile["supports_vision"] = False
    repository.update_model_profile(profile, "2026-10-20T00:00:00Z")
    initialize_database(factory)
    assert repository.get_model_profile("old")["supports_vision"] is False
