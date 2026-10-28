from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Event, get_ident
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api_server.services.document_pipeline_service as pipeline_module
import app.api_server.services.model_config_service as model_config_service
import app.api_server.services.workspace_store as workspace_store
from app.api_server.apis.v1.schemas import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseUpdateRequest,
    ModelProfileRequest,
)
from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.main import create_app
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.document_pipeline_service import (
    DocumentPipelineService,
    _LimitedCachedLLM,
    _load_enriched_generation_artifacts,
    _parse_enrichment_diagnostics,
)
from app.api_server.services.knowledge_base_service import KnowledgeBaseService
from app.api_server.services.model_config_service import ModelConfigService


@pytest.fixture
def enrichment_services(
    tmp_path: Path,
) -> Iterator[tuple[DocumentPipelineService, str]]:
    factory = SQLiteConnectionFactory(tmp_path / "metadata.sqlite3")
    initialize_database(factory)
    repository = SQLiteMetadataRepository(factory)
    knowledge_bases = KnowledgeBaseService(
        repository, tmp_path / "workspaces", ModelConfigService(repository)
    )
    kb = knowledge_bases.create(
        KnowledgeBaseCreateRequest(
            name="consistent enrichment settings",
            summary_enabled=False,
            heading_recovery_enabled=False,
            min_subtree_tokens=1,
        )
    )
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": kb.id,
            "original_filename": "source.md",
            "file_extension": ".md",
            "size_bytes": 8,
            "source_relpath": "sources/source.md",
            "source_sha256": "source-hash",
            "parser": "native_markdown",
            "status": "parsed",
            "created_at": "2026-10-26T00:00:00+00:00",
            "updated_at": "2026-10-26T00:00:00+00:00",
        }
    )
    pipeline = object.__new__(DocumentPipelineService)
    pipeline.repository = repository
    pipeline.knowledge_bases = knowledge_bases
    pipeline._loop = None
    pipeline._wake_event = None
    try:
        yield pipeline, kb.id
    finally:
        factory.dispose()


@pytest.mark.parametrize("metadata_fails", [False, True], ids=["commit", "rollback"])
def test_enrichment_snapshot_waits_for_complete_settings_update(
    enrichment_services: tuple[DocumentPipelineService, str],
    monkeypatch: pytest.MonkeyPatch,
    metadata_fails: bool,
) -> None:
    pipeline, kb_id = enrichment_services
    repository = pipeline.repository
    knowledge_bases = pipeline.knowledge_bases
    metadata_pending = Event()
    resume_update = Event()
    queue_thread = get_ident()
    original_update = repository.update_knowledge_base_settings
    original_create = repository.create_enrichment_task
    original_lock_for = workspace_store._lock_for

    def pause_metadata_update(
        knowledge_base_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        metadata_pending.set()
        assert resume_update.wait(5), "enrichment reader did not resume update"
        if metadata_fails:
            raise RuntimeError("metadata unavailable")
        return original_update(knowledge_base_id, updates)

    def resume_on_queue_lock(path: Path):
        if get_ident() == queue_thread and metadata_pending.is_set():
            resume_update.set()
        return original_lock_for(path)

    def record_task_before_resuming_update(**kwargs: Any) -> dict[str, Any]:
        try:
            return original_create(**kwargs)
        finally:
            # Also let the unprotected implementation finish, so RED reports
            # the mixed durable snapshot rather than a synchronization timeout.
            resume_update.set()

    monkeypatch.setattr(
        repository, "update_knowledge_base_settings", pause_metadata_update
    )
    monkeypatch.setattr(
        repository, "create_enrichment_task", record_task_before_resuming_update
    )
    monkeypatch.setattr(workspace_store, "_lock_for", resume_on_queue_lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        update = executor.submit(
            knowledge_bases.update,
            kb_id,
            KnowledgeBaseUpdateRequest(
                summary_enabled=True,
                heading_recovery_enabled=True,
                min_subtree_tokens=1200,
            ),
        )
        try:
            assert metadata_pending.wait(5), "settings update did not reach metadata"
            pipeline._queue_enrichment("doc-1", 1, "normalized-hash")
        finally:
            resume_update.set()
        if metadata_fails:
            with pytest.raises(RuntimeError, match="metadata unavailable"):
                update.result(timeout=5)
        else:
            assert update.result(timeout=5).min_subtree_tokens == 1200

    task = repository.get_latest_enrichment_task("doc-1", 1)
    assert task is not None
    assert json.loads(task["options_json"]) == {
        "summary_enabled": not metadata_fails,
        "heading_recovery_enabled": not metadata_fails,
        "subtree_folding_enabled": True,
        "min_subtree_tokens": 1 if metadata_fails else 1200,
        "reasoning_effort": None,
        "temperature": None,
    }


def test_enrichment_options_and_model_snapshot_are_fenced_through_enqueue(
    enrichment_services: tuple[DocumentPipelineService, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, kb_id = enrichment_services
    repository = pipeline.repository
    knowledge_bases = pipeline.knowledge_bases
    models = knowledge_bases.models
    profiles = [
        models.create_profile(
            ModelProfileRequest(
                kind="llm", name=name, model=name, base_url="https://llm.test/v1"
            )
        )
        for name in ("old-model", "new-model")
    ]
    knowledge_bases.update(
        kb_id, KnowledgeBaseUpdateRequest(llm_model_id=profiles[0].id)
    )
    enqueue_pending = Event()
    resume_enqueue = Event()
    original_create = repository.create_enrichment_task
    original_lock_for = workspace_store._lock_for

    def pause_enqueue(**kwargs: Any) -> dict[str, Any]:
        enqueue_pending.set()
        assert resume_enqueue.wait(5), "settings writer did not resume enqueue"
        return original_create(**kwargs)

    def resume_on_settings_lock(path: Path):
        if get_ident() == settings_thread and enqueue_pending.is_set():
            resume_enqueue.set()
        return original_lock_for(path)

    monkeypatch.setattr(repository, "create_enrichment_task", pause_enqueue)
    monkeypatch.setattr(workspace_store, "_lock_for", resume_on_settings_lock)
    settings_thread = get_ident()
    with ThreadPoolExecutor(max_workers=1) as executor:
        enqueue = executor.submit(
            pipeline._queue_enrichment, "doc-1", 1, "normalized-hash"
        )
        try:
            assert enqueue_pending.wait(5), "enrichment did not reach enqueue"
            updated = knowledge_bases.update(
                kb_id,
                KnowledgeBaseUpdateRequest(
                    summary_enabled=True,
                    heading_recovery_enabled=True,
                    llm_model_id=profiles[1].id,
                ),
            )
            assert updated.summary_enabled is True
        finally:
            # Also release implementations that update SQLite without the lock.
            resume_enqueue.set()
        enqueue.result(timeout=5)

    task = repository.get_latest_enrichment_task("doc-1", 1)
    assert task is not None
    assert task["llm_profile_id"] == profiles[0].id
    profile = repository.get_model_profile(profiles[0].id)
    assert profile is not None
    assert task["llm_profile_updated_at"] == profile["updated_at"]
    assert json.loads(task["options_json"]) == {
        "summary_enabled": False,
        "heading_recovery_enabled": False,
        "subtree_folding_enabled": True,
        "min_subtree_tokens": 1,
        "reasoning_effort": None,
        "temperature": None,
    }


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
        lambda _self, _profile_id=None, **_kwargs: delegate,
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
