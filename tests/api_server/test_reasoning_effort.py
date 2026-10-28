from __future__ import annotations

import json
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api_server.services.model_config_service as model_module
from app.api_server.config import ApiServerSettings
from app.api_server.database import initialize_database
from app.api_server.main import create_app


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


def _model(client, **updates):
    payload = {
        "kind": "llm",
        "name": "reasoning",
        "model": "reasoning-model",
        "base_url": "https://model.test/v1",
        "api_key": "test-only-key",
        **updates,
    }
    result = client.post("/api/v1/models", json=payload)
    assert result.status_code == 200, result.text
    return result.json()["data"], payload


def test_model_effort_persists_and_omitted_updates_preserve_it(client):
    profile, payload = _model(client, reasoning_effort="high")
    path = f"/api/v1/models/{profile['id']}"
    assert profile["reasoning_effort"] == "high"
    assert client.get(path).json()["data"]["reasoning_effort"] == "high"
    payload.pop("reasoning_effort")
    assert client.put(path, json=payload).json()["data"]["reasoning_effort"] == "high"
    payload["reasoning_effort"] = None
    assert client.put(path, json=payload).json()["data"]["reasoning_effort"] is None


@pytest.mark.parametrize("value", ["inherit", "default", "unknown", True, 2])
def test_model_rejects_invalid_effort(client, value):
    response = client.post(
        "/api/v1/models",
        json={
            "kind": "llm",
            "name": "invalid",
            "model": "test",
            "base_url": "https://model.test/v1",
            "reasoning_effort": value,
        },
    )
    assert response.status_code == 422


def test_non_llm_rejects_effort(client):
    response = client.post(
        "/api/v1/models",
        json={
            "kind": "embedding",
            "name": "embedding",
            "model": "test",
            "base_url": "https://model.test/v1",
            "reasoning_effort": "high",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("temperature", [None, 0.0, 0.7, 2.0])
def test_model_temperature_round_trip_and_omitted_update(client, temperature):
    profile, payload = _model(client, temperature=temperature)
    assert profile["temperature"] == temperature
    path = f"/api/v1/models/{profile['id']}"
    payload.pop("temperature")
    assert client.put(path, json=payload).json()["data"]["temperature"] == temperature
    payload["temperature"] = None
    assert client.put(path, json=payload).json()["data"]["temperature"] is None


@pytest.mark.parametrize("temperature", [-0.1, 2.1, "0.5", True])
def test_model_rejects_invalid_temperature(client, temperature):
    response = client.post(
        "/api/v1/models",
        json={
            "kind": "llm",
            "name": "invalid",
            "model": "test",
            "base_url": "https://model.test/v1",
            "temperature": temperature,
        },
    )
    assert response.status_code == 422


def test_non_llm_rejects_temperature(client):
    response = client.post(
        "/api/v1/models",
        json={
            "kind": "embedding",
            "name": "embedding",
            "model": "test",
            "base_url": "https://model.test/v1",
            "temperature": 0,
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("temperature", [None, 0.0, 0.8])
def test_model_build_and_probe_keep_temperature_independent_of_reasoning(
    client, monkeypatch, temperature
):
    profile, _ = _model(client, reasoning_effort="high", temperature=temperature)
    calls = []

    class LLM:
        def invoke(self, _prompt):
            return "OK"

    def build(**kwargs):
        calls.append(kwargs)
        return LLM()

    monkeypatch.setattr(model_module, "build_chat_model", build)
    models = client.app.state.services.models
    models.build_llm(profile["id"])
    assert calls[-1]["temperature"] == temperature
    assert calls[-1]["reasoning_effort"] == "high"
    models.build_llm(profile["id"], temperature=None)
    assert calls[-1]["temperature"] is None
    url = f"/api/v1/models/{profile['id']}/test"
    assert client.post(url).json()["data"]["ok"] is True
    assert calls[-1]["temperature"] == temperature
    assert (
        client.post(url, json={"target": "llm", "temperature": None}).json()["data"][
            "ok"
        ]
        is True
    )
    assert calls[-1]["temperature"] is None


def test_catalog_build_and_probe_apply_saved_and_explicit_default_effort(
    client, monkeypatch
):
    profile, _ = _model(client, reasoning_effort="high")
    calls = []

    class LLM:
        def invoke(self, _prompt):
            return "OK"

    def build(**kwargs):
        calls.append(kwargs)
        return LLM()

    monkeypatch.setattr(model_module, "build_chat_model", build)
    models = client.app.state.services.models
    models.build_llm(profile["id"])
    assert calls[-1]["reasoning_effort"] == "high"
    models.build_llm(profile["id"], reasoning_effort="default")
    assert "reasoning_effort" not in calls[-1]
    url = f"/api/v1/models/{profile['id']}/test"
    assert client.post(url).json()["data"]["ok"] is True
    assert calls[-1]["reasoning_effort"] == "high"
    assert (
        client.post(url, json={"target": "llm", "reasoning_effort": None}).json()[
            "data"
        ]["ok"]
        is True
    )
    assert "reasoning_effort" not in calls[-1]


@pytest.mark.parametrize(
    "override, expected", [("inherit", "high"), ("default", None), ("low", "low")]
)
@pytest.mark.parametrize("temperature", [None, 0.0, 0.8])
def test_knowledge_base_overrides_and_queued_task_snapshot(
    client, override, expected, temperature
):
    profile, payload = _model(client, reasoning_effort="high", temperature=temperature)
    kb = client.post(
        "/api/v1/knowledge-bases",
        json={
            "name": "KB",
            "llm_model_id": profile["id"],
            "reasoning_effort": override,
            "summary_enabled": False,
        },
    ).json()["data"]
    assert kb["reasoning_effort"] == override
    repository = client.app.state.services.models.repository
    repository.create_document(
        {
            "id": "doc",
            "knowledge_base_id": kb["id"],
            "original_filename": "doc.md",
            "file_extension": ".md",
            "size_bytes": 1,
            "source_relpath": "doc.md",
            "source_sha256": "hash",
            "parser": "native_markdown",
            "created_at": "2026-10-27",
            "updated_at": "2026-10-27",
        }
    )
    task = repository.create_enrichment_task(
        document_id="doc",
        pipeline_generation=1,
        input_sha256="hash",
        llm_profile_id=None,
        llm_profile_updated_at=None,
        use_knowledge_base_model=True,
        prompt_schema_version=1,
        options_json=json.dumps(
            {
                "summary_enabled": False,
                "heading_recovery_enabled": False,
                "subtree_folding_enabled": False,
                "min_subtree_tokens": 1,
            }
        ),
        now="2026-10-27",
    )
    assert json.loads(task["options_json"])["reasoning_effort"] == expected
    assert json.loads(task["options_json"])["temperature"] == temperature
    payload["temperature"] = 1.2
    assert (
        client.put(f"/api/v1/models/{profile['id']}", json=payload).status_code == 200
    )
    response = client.patch(
        f"/api/v1/knowledge-bases/{kb['id']}", json={"reasoning_effort": "medium"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["content_version"] == kb["content_version"]
    saved = repository.get_enrichment_task(task["id"])
    assert json.loads(saved["options_json"])["reasoning_effort"] == expected
    assert json.loads(saved["options_json"])["temperature"] == temperature


def test_old_database_migration_adds_defaults_and_is_repeatable(client):
    profile, _ = _model(client)
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "legacy", "summary_enabled": False}
    ).json()["data"]
    app = client.post(
        "/api/v1/apps", json={"name": "legacy", "knowledge_base_id": kb["id"]}
    ).json()["data"]
    factory = client.app.state.services.models.repository.factory
    with closing(factory.connect()) as connection:
        for table in ("llm_model_profiles", "knowledge_bases", "applications"):
            connection.execute(f'ALTER TABLE "{table}" DROP COLUMN reasoning_effort')
        connection.execute("ALTER TABLE llm_model_profiles DROP COLUMN temperature")
    initialize_database(factory)
    assert (
        client.get(f"/api/v1/models/{profile['id']}").json()["data"]["temperature"]
        is None
    )
    initialize_database(factory)
    assert (
        client.get(f"/api/v1/models/{profile['id']}").json()["data"]["reasoning_effort"]
        is None
    )
    assert (
        client.get(f"/api/v1/knowledge-bases/{kb['id']}").json()["data"][
            "reasoning_effort"
        ]
        == "inherit"
    )
    assert (
        client.get(f"/api/v1/apps/{app['id']}").json()["data"]["reasoning_effort"]
        == "inherit"
    )


@pytest.mark.parametrize(
    "override, expected", [("inherit", "high"), ("default", None), ("low", "low")]
)
def test_agent_runtime_uses_its_own_override_not_knowledge_base_setting(
    client, override, expected, monkeypatch
):
    profile, _ = _model(client, reasoning_effort="high", temperature=0.4)
    kb = client.post(
        "/api/v1/knowledge-bases",
        json={
            "name": "KB",
            "llm_model_id": profile["id"],
            "reasoning_effort": "medium",
            "summary_enabled": False,
        },
    ).json()["data"]
    response = client.post(
        "/api/v1/apps",
        json={
            "name": "Agent",
            "knowledge_base_id": kb["id"],
            "llm_model_id": profile["id"],
            "reasoning_effort": override,
        },
    )
    assert response.status_code == 200, response.text
    agent = response.json()["data"]
    applications = client.app.state.services.applications
    applications.runtime_factory = lambda **kwargs: SimpleNamespace(**kwargs)
    models = client.app.state.services.models
    original_require = models.require_llm_profile
    selected_profiles = []

    def require_once(profile_id):
        selected_profiles.append(profile_id)
        return original_require(profile_id)

    monkeypatch.setattr(models, "require_llm_profile", require_once)
    runtime = applications.runtime(agent["id"])
    assert runtime.reasoning_effort == expected
    assert runtime.temperature == 0.4
    assert selected_profiles == [profile["id"]]
    update = client.patch(
        f"/api/v1/apps/{agent['id']}",
        json={
            "expected_config_version": agent["config_version"],
            "reasoning_effort": "medium",
        },
    )
    assert update.status_code == 200, update.text
    changed = applications.runtime(agent["id"])
    assert changed is not runtime
    assert changed.reasoning_effort == "medium"


@pytest.mark.parametrize("temperature", [None, 0.0, 0.7])
@pytest.mark.parametrize("legacy", [False, True])
def test_document_worker_uses_enqueued_temperature_after_profile_changes(
    client, monkeypatch, temperature, legacy
):
    profile, _ = _model(client, reasoning_effort="high", temperature=temperature)
    kb = client.post(
        "/api/v1/knowledge-bases",
        json={
            "name": "worker",
            "llm_model_id": profile["id"],
            "summary_enabled": True,
        },
    ).json()["data"]
    models = client.app.state.services.models
    original_build = models.build_llm
    calls = []
    if legacy:
        repository = models.repository
        original_claim = repository.claim_enrichment_task

        def claim_legacy(*args, **kwargs):
            task = original_claim(*args, **kwargs)
            if task is not None:
                options = json.loads(task["options_json"])
                options.pop("reasoning_effort", None)
                options.pop("temperature", None)
                task["options_json"] = json.dumps(options)
            return task

        monkeypatch.setattr(repository, "claim_enrichment_task", claim_legacy)

    class LLM:
        async def ainvoke(self, _prompt, **kwargs):
            assert "temperature" not in kwargs
            return "A document summary"

    def build_chat(**kwargs):
        calls.append(kwargs)
        return LLM()

    def build_after_profile_change(profile_id=None, **kwargs):
        changed = models.repository.get_model_profile(profile["id"])
        changed["temperature"] = 1.3
        models.repository.update_model_profile(changed, "2026-10-27T01:00:00+00:00")
        return original_build(profile_id, **kwargs)

    monkeypatch.setattr(models, "build_llm", build_after_profile_change)
    monkeypatch.setattr(model_module, "build_chat_model", build_chat)
    uploaded = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={
            "file": ("worker.md", b"# Heading\n\nSome document content."),
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    document_id = uploaded.json()["data"]["document_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        document = client.get(
            f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}"
        ).json()["data"]
        if document["status"] in {"ready", "failed"}:
            break
        time.sleep(0.01)
    assert document["status"] == "ready", document
    assert calls
    assert calls[0]["temperature"] == (None if legacy else temperature)
    assert calls[0]["reasoning_effort"] == "high"
