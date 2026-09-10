from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api_server.services.document_pipeline_service as pipeline_module
import app.api_server.services.model_config_service as model_config_service
from app.api_server.config import ApiServerSettings
from app.api_server.main import create_app
from app.api_server.services.document_pipeline_service import (
    DocumentPipelineService,
    _LimitedCachedLLM,
    _load_enriched_generation_artifacts,
    _parse_enrichment_diagnostics,
)


class _CallResultRepository:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    def get_enrichment_call_result(self, **_kwargs: Any) -> None:
        return None

    def put_enrichment_call_result(self, **values: Any) -> None:
        self.saved.append(values)


class _HangingLLM:
    def __init__(self) -> None:
        self.cancelled = False

    async def ainvoke(self, _prompt: Any, **_kwargs: Any) -> str:
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True
        return "unreachable"


def test_limited_llm_applies_hard_timeout_and_records_failure() -> None:
    async def scenario() -> None:
        repository = _CallResultRepository()
        delegate = _HangingLLM()
        llm = _LimitedCachedLLM(
            delegate,
            repository,  # type: ignore[arg-type]
            document_id="doc-1",
            pipeline_generation=1,
            model_fingerprint="model",
            global_semaphore=asyncio.Semaphore(1),
            per_document_limit=1,
            request_timeout_seconds=0.01,
        )

        with pytest.raises(TimeoutError, match="LLM 请求超过 0.01 秒"):
            await llm.ainvoke("Partial Document Text: body")

        assert delegate.cancelled
        assert llm.failed == 1
        assert repository.saved[-1]["state"] == "failed"
        assert repository.saved[-1]["error_code"] == "TimeoutError"

    asyncio.run(scenario())


class _PeriodicWorkerRepository:
    def __init__(self) -> None:
        self.delivered = False
        self.renewals = 0

    def claim_normalization_task(self, **_kwargs: Any) -> None:
        return None

    def claim_enrichment_task(self, **_kwargs: Any) -> dict[str, str] | None:
        if self.delivered:
            return None
        self.delivered = True
        return {"id": "enrich-1", "lease_token": "token-1"}

    def renew_pipeline_task_lease(self, *_args: Any, **_kwargs: Any) -> bool:
        self.renewals += 1
        return True


class _RecoveryRepository:
    def list_unqueued_enrichment_documents(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "doc-1",
                "pipeline_generation": 1,
            }
        ]

    def get_latest_enrichment_task(self, *_args: Any) -> None:
        return None

    def get_latest_normalization_task(self, *_args: Any) -> dict[str, str]:
        return {"state": "succeeded", "output_sha256": "normalized-hash"}


def test_worker_periodically_scans_and_heartbeats_without_wake_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        service = object.__new__(DocumentPipelineService)
        repository = _PeriodicWorkerRepository()
        service.repository = repository  # type: ignore[assignment]
        service.settings = ApiServerSettings(pipeline_lease_seconds=1)
        service.owner = "test-worker"
        service._wake_event = asyncio.Event()
        completed = asyncio.Event()

        async def slow_enrich(_task: dict[str, Any]) -> None:
            await asyncio.sleep(0.45)
            completed.set()

        monkeypatch.setattr(service, "_enrich", slow_enrich)
        worker = asyncio.create_task(service._worker(0))
        try:
            await asyncio.wait_for(completed.wait(), timeout=1.5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        assert repository.delivered
        assert repository.renewals >= 1

    asyncio.run(scenario())


def test_recover_requeues_enrichment_missing_after_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(DocumentPipelineService)
    service.repository = _RecoveryRepository()  # type: ignore[assignment]
    service._loop = None
    service._wake_event = None
    queued: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        service,
        "_queue_enrichment",
        lambda document_id, generation, output_sha256: queued.append(
            (document_id, generation, output_sha256)
        ),
    )

    service.recover()

    assert queued == [("doc-1", 1, "normalized-hash")]


def test_enrich_retry_reuses_complete_generation_artifacts(tmp_path: Path) -> None:
    enriched = tmp_path / "artifacts/doc-1/g1/enriched"
    enriched.mkdir(parents=True)
    diagnostics = {
        "nodes_total": 2,
        "model_requests": 3,
        "model_requests_succeeded": 3,
        "model_requests_failed": 0,
        "model_node_requests_failed": 0,
    }
    (enriched / "full.md").write_bytes(b"# Title\n")
    (enriched / "tree.json").write_bytes(b'{"id":"doc-1"}')
    (enriched / "diagnostics.json").write_text(json.dumps(diagnostics))

    artifacts = _load_enriched_generation_artifacts(tmp_path, "doc-1", 1)

    assert artifacts is not None
    assert artifacts["full.md"] == b"# Title\n"
    assert _parse_enrichment_diagnostics(artifacts["diagnostics.json"]) == diagnostics


def test_enrich_reuses_staged_artifacts_after_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Repository:
        def append_pipeline_event(self, **_kwargs: Any) -> None:
            return None

    class KnowledgeBases:
        def require_record(self, _knowledge_base_id: str) -> dict[str, str]:
            return {"workspace_dir": str(tmp_path)}

    async def scenario() -> None:
        document = {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "parser": "native_markdown",
            "file_extension": ".md",
            "source_relpath": "sources/doc-1.md",
            "mime_type": "text/markdown",
        }
        raw = b"# Title\n"
        normalized = tmp_path / "artifacts/doc-1/g1/normalized"
        normalized.mkdir(parents=True)
        (normalized / "full.md").write_bytes(raw)
        enriched = tmp_path / "artifacts/doc-1/g1/enriched"
        enriched.mkdir(parents=True)
        (enriched / "full.md").write_bytes(raw)
        (enriched / "tree.json").write_bytes(b'{"id":"doc-1"}')
        (enriched / "diagnostics.json").write_text(
            json.dumps(
                {
                    "nodes_total": 1,
                    "model_requests": 2,
                    "model_requests_succeeded": 2,
                    "model_requests_failed": 0,
                    "model_node_requests_failed": 0,
                }
            )
        )
        service = object.__new__(DocumentPipelineService)
        service.repository = Repository()  # type: ignore[assignment]
        service.knowledge_bases = KnowledgeBases()  # type: ignore[assignment]
        service.settings = ApiServerSettings(fts_enabled=False)
        service._document = lambda _document_id: document
        service._upload_key = lambda _document: None
        service._schedule_indexes = lambda _knowledge_base_id: None
        service.models = object()  # type: ignore[assignment]
        published: dict[str, Any] = {}
        monkeypatch.setattr(
            pipeline_module,
            "publish_document_snapshot",
            lambda *_args, **kwargs: published.update(kwargs) or 2,
        )

        await service._enrich(
            {
                "id": "enrich-1",
                "document_id": "doc-1",
                "pipeline_generation": 1,
                "lease_token": "token",
                "input_sha256": pipeline_module._sha256(raw),
                "options_json": "{}",
            }
        )

        assert published["tree_content"] == b'{"id":"doc-1"}'
        assert published["enrichment_completion"]["node_count"] == 1

    asyncio.run(scenario())


def test_llm_request_timeout_setting_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIANLUN_API_LLM_REQUEST_TIMEOUT_SECONDS", "12.5")

    assert ApiServerSettings.from_env().llm_request_timeout_seconds == 12.5
    with pytest.raises(ValueError, match="LLM 请求超时时间"):
        ApiServerSettings(llm_request_timeout_seconds=0)


def test_hanging_llm_does_not_leave_document_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = _HangingLLM()
    monkeypatch.setattr(
        model_config_service.ModelConfigService,
        "build_llm",
        lambda _self: delegate,
    )
    client = TestClient(
        create_app(
            ApiServerSettings(
                data_dir=tmp_path / "api",
                workspace_root=tmp_path / "workspaces",
                fts_enabled=False,
                pipeline_lease_seconds=1,
                llm_request_timeout_seconds=0.05,
            )
        )
    )
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "模型超时", "summary_enabled": True},
    ).json()["data"]["id"]
    upload = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={
            "file": (
                "timeout.md",
                ("# Timeout\n\n" + "content " * 400).encode(),
            )
        },
    )
    document_id = upload.json()["data"]["document_id"]

    document: dict[str, Any] = {}
    for _ in range(300):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        ).json()["data"]
        if document.get("status") == "ready":
            break
        time.sleep(0.01)

    assert document["status"] == "ready"
    assert document["stage_state"] == "partial"
    assert document["failed_stage"] is None
    assert document["warnings"] == [{"warning_code": "LLM_PARTIAL_FAILURE", "count": 2}]
