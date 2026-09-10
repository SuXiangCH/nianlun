from __future__ import annotations

import hashlib
import json

from nianlun.indexing.fts.config import FTS_SCHEMA_CHECK_TIMEOUT_SECONDS
from nianlun.indexing.fts.store import CollectionSchemaStatus
from nianlun.knowledgebase.config import KnowledgeBaseConfig
from nianlun.knowledgebase.factory import KnowledgeBaseFactory


class _FullTextStore:
    collection = "fts"
    schema_status_value = CollectionSchemaStatus.CURRENT
    schema_probe_timeouts: list[float | None] = []

    def schema_status(self, *, timeout: float | None = None) -> CollectionSchemaStatus:
        self.schema_probe_timeouts.append(timeout)
        return self.schema_status_value


class _FullTextSearcher:
    def __init__(self, **_kwargs) -> None:
        self.store = _FullTextStore()


def _empty_snapshot(tmp_path, *, knowledge_base_id: str = "kb", revision: int = 0):
    snapshot = tmp_path / "snapshots" / f"r{revision}"
    snapshot.mkdir(parents=True)
    payload = json.dumps(
        {
            "schema_version": 2,
            "knowledge_base_id": knowledge_base_id,
            "content_version": revision,
            "documents": [],
            "artifact_files": [],
        },
        sort_keys=True,
    ).encode()
    (snapshot / "manifest.json").write_bytes(payload)
    return f"snapshots/r{revision}", hashlib.sha256(payload).hexdigest()


def test_vector_backend_failure_degrades_without_semantic_retriever(
    monkeypatch, tmp_path
):
    _FullTextStore.schema_status_value = CollectionSchemaStatus.CURRENT
    _FullTextStore.schema_probe_timeouts.clear()
    (tmp_path / "_meta.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "nianlun.knowledgebase.factory.FullTextNodeRetriever",
        _FullTextSearcher,
    )

    def fail_embedding_client(**_kwargs):
        raise ConnectionError("embedding backend unavailable")

    monkeypatch.setattr(
        "nianlun.knowledgebase.factory.build_embedding_client",
        fail_embedding_client,
    )

    factory = KnowledgeBaseFactory(
        KnowledgeBaseConfig(
            workspace_dir=tmp_path,
            vector_enabled=True,
            embedding_dim=1024,
        )
    )

    runtime_kb = factory.create(
        api_key="test-key",
        base_url="https://example.test/v1",
        allow_env_fallback=False,
    )

    assert runtime_kb.has_fts is True
    assert runtime_kb.has_vector is False
    assert _FullTextStore.schema_probe_timeouts == [FTS_SCHEMA_CHECK_TIMEOUT_SECONDS]


def test_fts_schema_failure_uses_committed_snapshot_local_scan(
    monkeypatch, tmp_path
) -> None:
    _FullTextStore.schema_status_value = CollectionSchemaStatus.MISSING
    _FullTextStore.schema_probe_timeouts.clear()
    monkeypatch.setattr(
        "nianlun.knowledgebase.factory.FullTextNodeRetriever",
        _FullTextSearcher,
    )
    snapshot_relpath, manifest_sha256 = _empty_snapshot(tmp_path)
    factory = KnowledgeBaseFactory(
        KnowledgeBaseConfig(
            workspace_dir=tmp_path,
            knowledge_base_id="kb",
            content_version=0,
            snapshot_relpath=snapshot_relpath,
            snapshot_manifest_sha256=manifest_sha256,
        )
    )

    runtime_kb = factory.create(api_key=None, base_url=None, allow_env_fallback=False)

    assert runtime_kb.has_fts is True
    assert runtime_kb.search_document_nodes(
        "anything"
    ) == runtime_kb._empty_document_search_result("anything")
    assert _FullTextStore.schema_probe_timeouts == [FTS_SCHEMA_CHECK_TIMEOUT_SECONDS]
