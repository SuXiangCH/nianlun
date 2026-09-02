from collections.abc import AsyncIterator, Iterator
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api_server.services.model_config_service as model_config_service
from app.api_server.apis.v1.schemas import ChatResponse
from app.api_server.config import ApiServerSettings
from app.api_server.main import create_app
from app.api_server.services.container import build_services
from app.api_server.services.workspace_store import TREE_BUILD_OPTIONS_FILENAME


class FakeSummaryLLM:
    def invoke(self, _prompt: str, **_kwargs: object) -> str:
        return "文档描述"

    async def ainvoke(self, _prompt: str, **_kwargs: object) -> str:
        return "节点摘要"


def _settings(tmp_path: Path) -> ApiServerSettings:
    return ApiServerSettings(
        data_dir=tmp_path / "api",
        workspace_root=tmp_path / "workspaces",
        # API route tests do not exercise FTS; keep them independent of Milvus.
        fts_enabled=False,
    )


def _create_profile(client: TestClient, kind: str) -> dict[str, Any]:
    payloads: dict[str, dict[str, Any]] = {
        "llm": {
            "name": "测试 LLM",
            "model": "test-chat",
            "base_url": "https://llm.test/v1",
            "api_key": "test-key",
        },
        "embedding": {
            "name": "测试 Embedding",
            "model": "test-embedding",
            "dimension": 768,
            "base_url": "https://embedding.test/v1",
            "api_key": "test-key",
        },
        "parser": {
            "name": "MinerU SaaS",
            "base_url": "https://mineru.test",
            "api_key": "test-key",
        },
    }
    response = client.post("/api/v1/models", json={"kind": kind, **payloads[kind]})
    assert response.status_code == 200
    return response.json()["data"]


def _make_bindable(client: TestClient, knowledge_base_id: str) -> None:
    services = client.app.state.services
    record = services.knowledge_bases.require_record(knowledge_base_id)
    revision = int(record.get("content_version", 0))
    record.update(
        {
            "fts_status": "ready",
            "fts_revision": revision,
            "fts_target_revision": revision,
            "fts_collection": f"test_{knowledge_base_id}",
        }
    )
    services.knowledge_bases.repository.put(
        "knowledge_bases", knowledge_base_id, record
    )
    services.applications.fts_enabled = True


def test_app_lifespan_shuts_down_background_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    services = build_services(settings)
    shutdown_calls: list[str] = []
    for service_name in ("chat", "documents", "fts", "vector"):
        monkeypatch.setattr(
            getattr(services, service_name),
            "shutdown",
            lambda name=service_name: shutdown_calls.append(name),
        )

    with TestClient(create_app(settings, services)) as client:
        assert client.get("/api/v1/knowledge-bases").status_code == 200

    assert shutdown_calls == ["chat", "documents", "fts", "vector"]


def test_knowledge_base_upload_and_app_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_config_service.ModelConfigService,
        "build_llm",
        lambda _self: FakeSummaryLLM(),
    )
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "测试知识库", "description": "Markdown test"},
    )
    assert response.status_code == 200
    knowledge_base = response.json()["data"]
    knowledge_base_id = knowledge_base["id"]
    assert knowledge_base["document_count"] == 0

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("report.md", b"# Revenue\n\nRevenue grew.")},
    )
    assert response.status_code == 200
    assert response.json()["data"]["document_count"] == 1
    assert response.json()["data"]["vector_enabled"] is False
    assert (Path(knowledge_base["workspace_dir"]) / "_meta.json").exists()
    _make_bindable(client, knowledge_base_id)
    llm = _create_profile(client, "llm")

    response = client.post(
        "/api/v1/apps",
        json={
            "name": "测试应用",
            "knowledge_base_id": knowledge_base_id,
            "llm_model_id": llm["id"],
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["knowledge_base_id"] == knowledge_base_id
    assert response.json()["data"]["llm_model_id"] == llm["id"]


def test_knowledge_base_summary_switch_defaults_on_and_can_be_disabled(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    created = client.post("/api/v1/knowledge-bases", json={"name": "摘要开关"}).json()[
        "data"
    ]
    _make_bindable(client, created["id"])
    knowledge_base_id = created["id"]
    assert created["summary_enabled"] is True

    updated = client.patch(
        f"/api/v1/knowledge-bases/{knowledge_base_id}",
        json={"summary_enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["summary_enabled"] is False

    fetched = client.get(f"/api/v1/knowledge-bases/{knowledge_base_id}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["summary_enabled"] is False


def test_new_knowledge_base_enables_subtree_folding_without_api_setting(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    new_knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "新知识库", "summary_enabled": False},
    ).json()["data"]
    new_workspace = Path(new_knowledge_base["workspace_dir"])
    options = json.loads(
        (new_workspace / TREE_BUILD_OPTIONS_FILENAME).read_text(encoding="utf-8")
    )
    assert options == {
        "min_subtree_tokens": 1200,
        "subtree_folding_enabled": True,
        "version": 1,
    }

    response = client.post(
        f"/api/v1/knowledge-bases/{new_knowledge_base['id']}/documents",
        files={"file": ("new.md", b"# Parent\n\nintro\n\n## Child\n\ndetail")},
    )
    assert response.status_code == 200
    new_document_id = response.json()["data"]["document_id"]
    new_artifact = json.loads(
        (new_workspace / f"{new_document_id}.json").read_text(encoding="utf-8")
    )
    assert len(new_artifact["structure"]) == 1
    assert not new_artifact["structure"][0].get("nodes")
    assert "## Child" in new_artifact["structure"][0]["text"]

    legacy_knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "旧知识库", "summary_enabled": False},
    ).json()["data"]
    legacy_workspace = Path(legacy_knowledge_base["workspace_dir"])
    # Existing workspaces created before this release have no private config file.
    (legacy_workspace / TREE_BUILD_OPTIONS_FILENAME).unlink()
    response = client.post(
        f"/api/v1/knowledge-bases/{legacy_knowledge_base['id']}/documents",
        files={"file": ("legacy.md", b"# Parent\n\nintro\n\n## Child\n\ndetail")},
    )
    assert response.status_code == 200
    legacy_document_id = response.json()["data"]["document_id"]
    legacy_artifact = json.loads(
        (legacy_workspace / f"{legacy_document_id}.json").read_text(encoding="utf-8")
    )
    assert legacy_artifact["structure"][0]["nodes"][0]["title"] == "Child"


def test_knowledge_base_name_can_be_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    services = build_services(settings)
    monkeypatch.setattr(
        services.applications,
        "invalidate_all",
        lambda: pytest.fail("renaming a knowledge base must preserve agent runtimes"),
    )
    client = TestClient(create_app(settings, services))
    created = client.post("/api/v1/knowledge-bases", json={"name": "旧名称"}).json()[
        "data"
    ]

    updated = client.patch(
        f"/api/v1/knowledge-bases/{created['id']}",
        json={"name": "  新名称  "},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["name"] == "新名称"

    fetched = client.get(f"/api/v1/knowledge-bases/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["name"] == "新名称"

    invalid = client.patch(
        f"/api/v1/knowledge-bases/{created['id']}", json={"name": "   "}
    )
    assert invalid.status_code == 422


def test_vector_toggle_preserves_index_and_model_switch_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    services = build_services(settings)
    client = TestClient(create_app(settings, services))
    first_model = _create_profile(client, "embedding")
    second_model = client.post(
        "/api/v1/models",
        json={
            "kind": "embedding",
            "name": "备用 Embedding",
            "model": "test-embedding-2",
            "dimension": 768,
            "base_url": "https://embedding-2.test/v1",
            "api_key": "test-key",
        },
    ).json()["data"]
    created = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "向量启停", "embedding_model_id": first_model["id"]},
    ).json()["data"]
    knowledge_base_id = created["id"]
    record = services.knowledge_bases.require_record(knowledge_base_id)
    record.update(
        {
            "vector_status": "ready",
            "vector_revision": 0,
            "vector_target_revision": 0,
            "vector_collection": "vector-existing",
            "vector_model_updated_at": first_model["updated_at"],
            "vector_dimension": 768,
        }
    )
    services.knowledge_bases.repository.put(
        "knowledge_bases", knowledge_base_id, record
    )

    disabled = client.patch(
        f"/api/v1/knowledge-bases/{knowledge_base_id}",
        json={"vector_enabled": False},
    )
    assert disabled.status_code == 200
    disabled_data = disabled.json()["data"]
    assert disabled_data["vector_enabled"] is False
    assert disabled_data["embedding_model_id"] == first_model["id"]
    assert disabled_data["vector_revision"] == 0
    assert disabled_data["vector_collection"] == "vector-existing"

    calls: list[tuple[str, bool, bool]] = []

    def fake_schedule(
        identifier: str, *, force: bool = False, activate: bool = False
    ) -> dict[str, Any]:
        calls.append((identifier, force, activate))
        current = services.knowledge_bases.require_record(identifier)
        current["vector_status"] = "pending"
        services.knowledge_bases.repository.put("knowledge_bases", identifier, current)
        return current

    monkeypatch.setattr(services.vector, "schedule", fake_schedule)
    enabled = client.patch(
        f"/api/v1/knowledge-bases/{knowledge_base_id}",
        json={"vector_enabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["vector_enabled"] is True
    assert calls == [(knowledge_base_id, False, True)]

    switched = client.patch(
        f"/api/v1/knowledge-bases/{knowledge_base_id}",
        json={"embedding_model_id": second_model["id"]},
    )
    assert switched.status_code == 200
    assert calls[-1] == (knowledge_base_id, True, True)
    assert switched.json()["data"]["vector_revision"] is None

    call_count = len(calls)
    disabled_with_model_change = client.patch(
        f"/api/v1/knowledge-bases/{knowledge_base_id}",
        json={
            "embedding_model_id": first_model["id"],
            "vector_enabled": False,
        },
    )
    assert disabled_with_model_change.status_code == 200
    assert disabled_with_model_change.json()["data"]["vector_enabled"] is False
    assert len(calls) == call_count


def test_vector_rebuild_endpoint_only_queues_a_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    services = build_services(settings)
    client = TestClient(create_app(settings, services))
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "向量索引 API"}
    ).json()["data"]["id"]
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        services.models,
        "embedding_runtime_config",
        lambda _profile_id, strict=True: {
            "enabled": True,
            "profile_id": "embedding-1",
            "profile_updated_at": "2026-08-11T00:00:01+00:00",
            "model": "embedding-model",
            "base_url": "https://embedding.example/v1",
            "api_key": "secret",
            "dimension": 768,
        },
    )
    monkeypatch.setattr(
        services.models,
        "require_embedding_profile",
        lambda _profile_id: {},
    )
    selected = client.patch(
        f"/api/v1/knowledge-bases/{knowledge_base_id}",
        json={"embedding_model_id": "embedding-1"},
    )
    assert selected.status_code == 200

    def fake_schedule(
        identifier: str, *, force: bool = False, activate: bool = False
    ) -> dict[str, Any]:
        calls.append((identifier, force))
        return services.knowledge_bases.require_record(identifier)

    monkeypatch.setattr(services.vector, "schedule", fake_schedule)
    response = client.post(f"/api/v1/knowledge-bases/{knowledge_base_id}/vector")

    assert response.status_code == 200
    assert calls == [(knowledge_base_id, True)]


def test_vector_rebuild_endpoint_rejects_disabled_embedding(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    services = build_services(settings)
    client = TestClient(create_app(settings, services))
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "未启用向量"}
    ).json()["data"]["id"]

    response = client.post(f"/api/v1/knowledge-bases/{knowledge_base_id}/vector")

    assert response.status_code == 409
    assert "选择 Embedding 模型" in response.text


def test_disabled_summary_upload_does_not_require_llm(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    created = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "纯结构索引", "summary_enabled": False},
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge-bases/{created['id']}/documents",
        files={"file": ("outline.md", "# Outline\n\n内容".encode("utf-8"))},
    )
    assert response.status_code == 200


def test_document_and_knowledge_base_delete_remove_persisted_data(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    created = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "可删除知识库", "summary_enabled": False},
    ).json()["data"]
    knowledge_base_id = created["id"]

    uploaded = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("notes.md", b"# Notes\n\ncontent")},
    )
    assert uploaded.status_code == 200
    document_id = uploaded.json()["data"]["document_id"]
    workspace = Path(created["workspace_dir"])
    manifest = json.loads((workspace / "_meta.json").read_text())
    source = workspace / manifest[document_id]["path"]
    assert source.exists()

    deleted_document = client.delete(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
    )
    assert deleted_document.status_code == 200
    assert (
        client.get(f"/api/v1/knowledge-bases/{knowledge_base_id}/documents").json()[
            "data"
        ]
        == []
    )
    assert not source.exists()
    assert json.loads((workspace / "_meta.json").read_text()) == {}
    assert (
        client.get(f"/api/v1/knowledge-bases/{knowledge_base_id}").json()["data"][
            "document_count"
        ]
        == 0
    )

    deleted_knowledge_base = client.delete(
        f"/api/v1/knowledge-bases/{knowledge_base_id}"
    )
    assert deleted_knowledge_base.status_code == 200
    assert not workspace.exists()
    assert client.get(f"/api/v1/knowledge-bases/{knowledge_base_id}").status_code == 404


def test_knowledge_base_delete_is_blocked_when_application_is_bound(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    _create_profile(client, "llm")
    created = client.post(
        "/api/v1/knowledge-bases", json={"name": "被应用使用", "summary_enabled": False}
    ).json()["data"]
    _make_bindable(client, created["id"])
    application = client.post(
        "/api/v1/apps",
        json={"name": "绑定应用", "knowledge_base_id": created["id"]},
    )
    assert application.status_code == 200

    response = client.delete(f"/api/v1/knowledge-bases/{created['id']}")
    assert response.status_code == 409
    assert "应用绑定" in response.json()["message"]
    assert client.get(f"/api/v1/knowledge-bases/{created['id']}").status_code == 200


def test_application_delete_keeps_knowledge_base_and_removes_application(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    _create_profile(client, "llm")
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "应用删除测试", "summary_enabled": False},
    ).json()["data"]
    _make_bindable(client, knowledge_base["id"])
    application = client.post(
        "/api/v1/apps",
        json={"name": "待删除应用", "knowledge_base_id": knowledge_base["id"]},
    ).json()["data"]

    response = client.delete(f"/api/v1/apps/{application['id']}")
    assert response.status_code == 200
    assert client.get(f"/api/v1/apps/{application['id']}").status_code == 404
    assert (
        client.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}").status_code == 200
    )
    assert client.delete(f"/api/v1/apps/{application['id']}").status_code == 404


def test_application_requires_llm_profile_and_protects_selected_model(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    _create_profile(client, "llm")
    embedding = _create_profile(client, "embedding")
    selected_llm = client.post(
        "/api/v1/models",
        json={
            "kind": "llm",
            "name": "Agent LLM",
            "model": "agent-chat",
            "base_url": "https://agent-llm.test/v1",
            "api_key": "agent-key",
        },
    ).json()["data"]
    knowledge_base = client.post(
        "/api/v1/knowledge-bases", json={"name": "Agent 模型约束"}
    ).json()["data"]
    _make_bindable(client, knowledge_base["id"])

    wrong_kind = client.post(
        "/api/v1/apps",
        json={
            "name": "错误模型",
            "knowledge_base_id": knowledge_base["id"],
            "llm_model_id": embedding["id"],
        },
    )
    assert wrong_kind.status_code == 422
    assert "只能选择 LLM" in wrong_kind.json()["message"]

    created = client.post(
        "/api/v1/apps",
        json={
            "name": "指定模型",
            "knowledge_base_id": knowledge_base["id"],
            "llm_model_id": selected_llm["id"],
        },
    )
    assert created.status_code == 200
    application = created.json()["data"]
    assert application["llm_model_id"] == selected_llm["id"]

    blocked = client.delete(f"/api/v1/models/{selected_llm['id']}")
    assert blocked.status_code == 409
    assert "Agent 使用" in blocked.json()["message"]

    assert client.delete(f"/api/v1/apps/{application['id']}").status_code == 200
    assert client.delete(f"/api/v1/models/{selected_llm['id']}").status_code == 200


def test_default_model_catalog_is_used_when_building_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://environment.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    settings = _settings(tmp_path)
    services = build_services(settings)
    client = TestClient(create_app(settings, services))
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "runtime 配置"}
    ).json()["data"]["id"]
    llm = _create_profile(client, "llm")
    embedding = _create_profile(client, "embedding")
    _make_bindable(client, knowledge_base_id)
    application_id = client.post(
        "/api/v1/apps",
        json={
            "name": "runtime app",
            "knowledge_base_id": knowledge_base_id,
        },
    ).json()["data"]["id"]
    response = client.put(
        f"/api/v1/models/{llm['id']}",
        json={
            "kind": "llm",
            "name": llm["name"],
            "model": "runtime-chat",
            "context_window_tokens": 128000,
            "base_url": "https://catalog-runtime.example/v1",
            "api_key": "runtime-secret",
            "is_default": True,
        },
    )
    assert response.status_code == 200
    selected = client.patch(
        f"/api/v1/knowledge-bases/{knowledge_base_id}",
        json={"embedding_model_id": embedding["id"]},
    )
    assert selected.status_code == 200
    response = client.put(
        f"/api/v1/models/{embedding['id']}",
        json={
            "kind": "embedding",
            "name": embedding["name"],
            "model": "runtime-embedding",
            "base_url": "https://catalog-embedding.example/v1",
            "dimension": 768,
            "enabled": True,
            "is_default": True,
        },
    )
    assert response.status_code == 200

    captured: dict[str, Any] = {}

    def fake_runtime(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    services.applications.runtime_factory = fake_runtime  # type: ignore[assignment]
    services.applications.runtime(application_id)

    assert captured["model"] == "runtime-chat"
    assert captured["base_url"] == "https://catalog-runtime.example/v1"
    assert captured["api_key"] == "runtime-secret"
    assert captured["context_window_tokens"] == 128000
    assert captured["allow_env_fallback"] is False
    assert captured["knowledge_base_config"].vector_enabled is False
    assert captured["knowledge_base_config"].embedding_model == "runtime-embedding"
    assert captured["knowledge_base_config"].embedding_dim == 768


def test_application_model_profile_overrides_default_profile(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    services = build_services(settings)
    client = TestClient(create_app(settings, services))
    llm = _create_profile(client, "llm")
    response = client.put(
        f"/api/v1/models/{llm['id']}",
        json={
            "kind": "llm",
            "name": llm["name"],
            "model": "default-profile-model",
            "base_url": "https://default.example/v1",
            "api_key": "default-key",
            "is_default": True,
        },
    )
    assert response.status_code == 200
    selected = client.post(
        "/api/v1/models",
        json={
            "kind": "llm",
            "name": "Agent 专用 LLM",
            "model": "agent-profile-model",
            "base_url": "https://agent.example/v1",
            "api_key": "agent-key",
        },
    ).json()["data"]

    model, provider, *_rest = services.models.runtime_config("default", selected["id"])

    assert model == "agent-profile-model"
    assert provider.base_url == "https://agent.example/v1"
    assert provider.api_key == "agent-key"


def test_api_runtime_does_not_fallback_to_environment_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://environment.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    settings = _settings(tmp_path)
    services = build_services(settings)
    client = TestClient(create_app(settings, services))
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "strict runtime"}
    ).json()["data"]["id"]
    llm = _create_profile(client, "llm")
    _make_bindable(client, knowledge_base_id)
    application_id = client.post(
        "/api/v1/apps",
        json={
            "name": "strict app",
            "knowledge_base_id": knowledge_base_id,
        },
    ).json()["data"]["id"]
    response = client.put(
        f"/api/v1/models/{llm['id']}",
        json={
            "kind": "llm",
            "name": llm["name"],
            "model": "catalog-chat",
            "base_url": "https://catalog.example/v1",
            "api_key": None,
            "is_default": True,
        },
    )
    assert response.status_code == 200

    captured: dict[str, Any] = {}

    def fake_runtime(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    services.applications.runtime_factory = fake_runtime  # type: ignore[assignment]
    services.applications.runtime(application_id)

    assert captured["model"] == "catalog-chat"
    assert captured["base_url"] == "https://catalog.example/v1"
    assert captured["api_key"] is None
    assert captured["allow_env_fallback"] is False


def test_model_catalog_crud_default_switch(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.get("/api/v1/models")
    assert response.status_code == 200
    initial = response.json()["data"]
    assert initial == []
    initial = [_create_profile(client, kind) for kind in ("llm", "embedding", "parser")]
    assert all(item["is_default"] for item in initial)

    response = client.post(
        "/api/v1/models",
        json={
            "kind": "llm",
            "name": "OpenAI 主模型",
            "model": "catalog-chat",
            "context_window_tokens": 128000,
            "base_url": "https://catalog.example/v1",
            "api_key": "catalog-secret",
        },
    )
    assert response.status_code == 200
    created = response.json()["data"]
    assert created["is_default"] is False
    assert created["context_window_tokens"] == 128000
    assert created["api_key_configured"] is True
    assert "catalog-secret" not in response.text

    response = client.put(
        f"/api/v1/models/{created['id']}",
        json={
            "kind": "llm",
            "name": "OpenAI 主模型（编辑后）",
            "model": "catalog-chat-v2",
            "context_window_tokens": 200000,
            "base_url": "https://catalog.example/v2",
            "is_default": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "OpenAI 主模型（编辑后）"
    assert response.json()["data"]["context_window_tokens"] == 200000
    assert response.json()["data"]["api_key_configured"] is True

    response = client.post(f"/api/v1/models/{created['id']}/default")
    assert response.status_code == 200
    assert response.json()["data"]["is_default"] is True
    active = next(
        item
        for item in client.get("/api/v1/models").json()["data"]
        if item["id"] == created["id"]
    )
    assert active["model"] == "catalog-chat-v2"
    assert active["context_window_tokens"] == 200000
    assert active["base_url"] == "https://catalog.example/v2"
    assert active["api_key_configured"] is True

    old_default = next(item for item in initial if item["kind"] == "llm")
    response = client.delete(f"/api/v1/models/{old_default['id']}")
    assert response.status_code == 200
    assert client.get(f"/api/v1/models/{old_default['id']}").status_code == 404


def test_model_catalog_url_is_required(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    response = client.post(
        "/api/v1/models",
        json={
            "kind": "llm",
            "name": "缺少 URL 的模型",
            "model": "catalog-chat",
        },
    )
    assert response.status_code == 422
    assert "base_url" in response.text

    response = client.post(
        "/api/v1/models",
        json={
            "kind": "llm",
            "name": "缺少模型名称",
            "base_url": "https://catalog.example/v1",
        },
    )
    assert response.status_code == 422
    assert "模型名称不能为空" in response.text


def test_model_catalog_connection_tests_use_mocked_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    calls: dict[str, Any] = {}

    class FakeChat:
        def invoke(self, prompt: str) -> object:
            calls["llm_prompt"] = prompt
            return object()

    class FakeEmbedding:
        def embed_query(self, text: str) -> list[float]:
            calls["embedding_text"] = text
            return [0.1, 0.2, 0.3]

    def fake_chat(**kwargs: Any) -> FakeChat:
        calls["llm"] = kwargs
        return FakeChat()

    def fake_embedding(**kwargs: Any) -> FakeEmbedding:
        calls["embedding"] = kwargs
        return FakeEmbedding()

    class FakeResponse:
        status_code = 404

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls["parser"] = {"url": url, **kwargs}
        return FakeResponse()

    monkeypatch.setattr(model_config_service, "build_chat_model", fake_chat)
    monkeypatch.setattr(model_config_service, "build_embeddings_model", fake_embedding)
    monkeypatch.setattr(model_config_service.httpx, "get", fake_get)

    response = client.post(
        "/api/v1/models/test",
        json={
            "target": "llm",
            "model": "draft-chat",
            "base_url": "https://draft.example/v1",
            "api_key": "draft-key",
        },
    )
    assert response.status_code == 200
    assert response.json()["data"] == {
        "target": "llm",
        "ok": True,
        "message": "LLM 连接成功",
    }
    assert calls["llm"] == {
        "model": "draft-chat",
        "base_url": "https://draft.example/v1",
        "api_key": "draft-key",
        "allow_env_fallback": False,
    }

    profiles = {}
    for kind, body in {
        "llm": {
            "name": "目录 LLM",
            "model": "catalog-chat",
            "base_url": "https://catalog-llm.example/v1",
            "api_key": "catalog-llm-key",
        },
        "embedding": {
            "name": "目录 Embedding",
            "model": "catalog-embedding",
            "dimension": 3,
            "base_url": "https://catalog-embedding.example/v1",
            "api_key": "catalog-embedding-key",
        },
        "parser": {
            "name": "目录 MinerU",
            "base_url": "https://catalog-mineru.example",
            "api_key": "catalog-mineru-key",
            "model_version": "vlm",
            "language": "ch",
        },
    }.items():
        response = client.post("/api/v1/models", json={"kind": kind, **body})
        assert response.status_code == 200
        profiles[kind] = response.json()["data"]

    for kind, profile in profiles.items():
        response = client.post(f"/api/v1/models/{profile['id']}/test")
        assert response.status_code == 200
        assert response.json()["data"] == {
            "target": kind,
            "ok": True,
            "message": (
                "LLM 连接成功"
                if kind == "llm"
                else "Embedding 连接成功，向量维度 3"
                if kind == "embedding"
                else "MinerU API 连接成功，查询接口可用"
            ),
        }

    assert calls["llm"] == {
        "model": "catalog-chat",
        "base_url": "https://catalog-llm.example/v1",
        "api_key": "catalog-llm-key",
        "allow_env_fallback": False,
    }
    assert calls["embedding"] == {
        "model": "catalog-embedding",
        "base_url": "https://catalog-embedding.example/v1",
        "api_key": "catalog-embedding-key",
        "dimensions": 3,
        "allow_env_fallback": False,
    }
    assert calls["parser"]["headers"] == {"Authorization": "Bearer catalog-mineru-key"}

    response = client.post(
        f"/api/v1/models/{profiles['llm']['id']}/test",
        json={
            "target": "llm",
            "model": "unsaved-chat",
            "base_url": "https://unsaved.example/v1",
            "api_key": "unsaved-key",
        },
    )
    assert response.status_code == 200
    assert calls["llm"] == {
        "model": "unsaved-chat",
        "base_url": "https://unsaved.example/v1",
        "api_key": "unsaved-key",
        "allow_env_fallback": False,
    }

    response = client.post(
        f"/api/v1/models/{profiles['llm']['id']}/test",
        json={"target": "llm", "model": "catalog-chat", "base_url": ""},
    )
    assert response.status_code == 200
    assert response.json()["data"] == {
        "target": "llm",
        "ok": False,
        "message": "模型 API URL 未配置",
    }


def test_missing_knowledge_base_is_a_domain_error(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    response = client.post(
        "/api/v1/apps",
        json={"name": "invalid", "knowledge_base_id": "missing"},
    )
    assert response.status_code == 404
    assert response.json()["data"] is None


class _FakeChatService:
    def complete(
        self,
        application_id: str,
        message: str,
        conversation_id: str | None,
        *,
        clarification_enabled: bool = False,
    ) -> ChatResponse:
        del clarification_enabled
        return ChatResponse(
            app_id=application_id,
            conversation_id=conversation_id or "conversation-1",
            message_id="message-1",
            answer=f"echo: {message}",
            route="direct",
            retrieved_snippets=[],
            status_events=[],
        )

    def stream(
        self,
        application_id: str,
        message: str,
        conversation_id: str | None,
        *,
        clarification_enabled: bool = False,
    ) -> tuple[str, str, Iterator[dict[str, Any]]]:
        del application_id, message, clarification_enabled
        return (
            conversation_id or "conversation-1",
            "message-1",
            iter(
                [
                    {
                        "type": "message",
                        "data": {
                            "delta": "我先检索相关文档。",
                            "phase": "candidate",
                            "round": 1,
                        },
                    },
                    {
                        "type": "trace",
                        "data": {
                            "kind": "agent_message",
                            "message": "我先检索相关文档。",
                            "round": 1,
                        },
                    },
                    {
                        "type": "message",
                        "data": {"delta": "hel", "phase": "candidate", "round": 2},
                    },
                    {
                        "type": "message",
                        "data": {"delta": "lo", "phase": "candidate", "round": 2},
                    },
                    {
                        "type": "done",
                        "data": {
                            "answer": "hello",
                            "route": "direct",
                            "retrieved_snippets": [],
                            "status_events": [],
                            "trace": [
                                {
                                    "kind": "agent_message",
                                    "message": "我先检索相关文档。",
                                    "round": 1,
                                }
                            ],
                        },
                    },
                ]
            ),
        )

    async def complete_async(self, *args: Any, **kwargs: Any) -> ChatResponse:
        return self.complete(*args, **kwargs)

    async def iterate_stream_async(
        self, events: Iterator[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        for event in events:
            yield event


def test_chat_blocking_and_streaming_contract(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    services = build_services(settings)
    services.chat = _FakeChatService()  # type: ignore[assignment]
    client = TestClient(create_app(settings, services))

    response = client.post(
        "/api/v1/apps/app-1/chat",
        json={"message": "hello", "response_mode": "blocking"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["answer"] == "echo: hello"
    assert "guard" not in response.json()["data"]

    response = client.post(
        "/api/v1/apps/app-1/chat",
        json={
            "message": "hello",
            "conversation_id": "conversation-1",
            "response_mode": "streaming",
        },
    )
    assert response.status_code == 200
    assert response.headers["X-Request-Id"]
    assert "event: ready" in response.text
    assert "event: trace" in response.text
    assert "event: candidate" not in response.text
    assert "event: message" in response.text
    assert '"phase":"candidate"' in response.text
    assert "event: done" in response.text
    assert '"guard"' not in response.text


def test_request_id_is_reused_and_logged(caplog: Any, tmp_path: Path) -> None:
    request_id = "frontend-request-001"
    client = TestClient(create_app(_settings(tmp_path)))

    with caplog.at_level(
        logging.INFO, logger="app.api_server.middleware.request_tracking"
    ):
        response = client.get(
            "/api/v1/healthz",
            headers={"X-Request-Id": request_id},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == request_id
    assert f"request.started request_id={request_id}" in caplog.text
    assert f"request.completed request_id={request_id}" in caplog.text
    assert "status_code=200" in caplog.text
    assert "duration_ms=" in caplog.text


def test_invalid_request_id_is_replaced(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.get(
        "/api/v1/healthz",
        headers={"X-Request-Id": "not safe for logs"},
    )

    generated_id = response.headers["X-Request-Id"]
    assert response.status_code == 200
    assert generated_id != "not safe for logs"
    assert len(generated_id) == 32
    assert all(character in "0123456789abcdef" for character in generated_id)


def test_document_tree_outline_serves_without_node_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_config_service.ModelConfigService,
        "build_llm",
        lambda _self: FakeSummaryLLM(),
    )
    client = TestClient(create_app(_settings(tmp_path)))
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "树测试"}
    ).json()["data"]["id"]
    upload = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("outline.md", b"# Alpha\n\nintro\n\n## Beta\n\ndetail")},
    ).json()["data"]
    document_id = upload["document_id"]

    response = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}/tree"
    )

    assert response.status_code == 200
    tree = response.json()["data"]
    assert tree["doc_name"] == "outline.md"
    assert tree["line_count"] == 7
    assert len(tree["structure"]) == 1
    alpha = tree["structure"][0]
    assert alpha["title"] == "Alpha"
    assert alpha["line_num"] == 1
    assert "text" not in alpha
    assert "nodes" not in alpha

    missing = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/no-such-doc/tree"
    )
    assert missing.status_code == 404
