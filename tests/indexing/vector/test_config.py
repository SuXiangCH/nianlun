from __future__ import annotations

from nianlun.indexing.vector.config import get_embedding_dim


def test_default_embedding_dimension(monkeypatch):
    monkeypatch.delenv("EMBEDDING_DIM", raising=False)
    assert get_embedding_dim() == 1024
