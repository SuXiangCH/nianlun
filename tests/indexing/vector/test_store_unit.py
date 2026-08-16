from __future__ import annotations

from nianlun.indexing.vector import store as store_module


class FakeClient:
    def __init__(self, **_kwargs):
        self.search_params = None

    def search(self, **kwargs):
        self.search_params = kwargs["search_params"]
        return [[]]

    def load_collection(self, _collection):
        return None


def test_vector_search_raises_hnsw_ef_for_large_candidate_limit(monkeypatch):
    fake_client = FakeClient()
    monkeypatch.setattr(store_module, "MilvusClient", lambda **kwargs: fake_client)
    monkeypatch.setattr(store_module, "ensure_pymilvus", lambda: None)

    store = store_module.DocVectorStore(collection_name="test", dimension=1)
    store.search([1.0], limit=512)

    assert fake_client.search_params["params"]["ef"] == 512


def test_vector_search_keeps_configured_ef_for_small_limit(monkeypatch):
    fake_client = FakeClient()
    monkeypatch.setattr(store_module, "MilvusClient", lambda **kwargs: fake_client)
    monkeypatch.setattr(store_module, "ensure_pymilvus", lambda: None)

    store = store_module.DocVectorStore(collection_name="test", dimension=1)
    store.search([1.0], limit=15)

    assert fake_client.search_params["params"]["ef"] == store_module.VECTOR_SEARCH_EF
