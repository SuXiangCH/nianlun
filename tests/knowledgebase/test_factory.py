from __future__ import annotations

import pytest

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


@pytest.mark.parametrize(
    ("schema_status", "error_message"),
    [
        (CollectionSchemaStatus.MISSING, "Milvus collection 不存在: fts"),
        (
            CollectionSchemaStatus.OUTDATED,
            "Milvus FTS collection schema 已过期；请等待或触发 FTS 索引重建",
        ),
    ],
)
def test_fts_schema_probe_preserves_specific_runtime_errors(
    monkeypatch,
    tmp_path,
    schema_status: CollectionSchemaStatus,
    error_message: str,
) -> None:
    _FullTextStore.schema_status_value = schema_status
    _FullTextStore.schema_probe_timeouts.clear()
    monkeypatch.setattr(
        "nianlun.knowledgebase.factory.FullTextNodeRetriever",
        _FullTextSearcher,
    )
    factory = KnowledgeBaseFactory(KnowledgeBaseConfig(workspace_dir=tmp_path))

    with pytest.raises(RuntimeError, match=error_message):
        factory.create(api_key=None, base_url=None, allow_env_fallback=False)

    assert _FullTextStore.schema_probe_timeouts == [FTS_SCHEMA_CHECK_TIMEOUT_SECONDS]
