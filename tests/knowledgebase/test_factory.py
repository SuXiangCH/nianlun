from __future__ import annotations

from nianlun.knowledgebase.config import KnowledgeBaseConfig
from nianlun.knowledgebase.factory import KnowledgeBaseFactory


class _CollectionClient:
    def has_collection(self, _collection: str) -> bool:
        return True


class _FullTextSearcher:
    def __init__(self, **_kwargs) -> None:
        self.store = type(
            "Store",
            (),
            {"client": _CollectionClient(), "collection": "fts"},
        )()


def test_vector_backend_failure_degrades_without_semantic_retriever(
    monkeypatch, tmp_path
):
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
