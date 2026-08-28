from __future__ import annotations

from typing import Any

from nianlun.indexing.fts.store import CollectionSchemaStatus, NodeFtsStore


CURRENT_FIELDS = [
    "doc_id",
    "doc_name",
    "source_type",
    "node_id",
    "title",
    "line_num",
    "text",
    "sparse",
    "node_summary",
    "node_summary_truncated",
]


class _SchemaClient:
    def __init__(self, fields: list[str], *, exists: bool = True) -> None:
        self.fields = fields
        self.exists = exists
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def has_collection(self, collection: str, **kwargs: Any) -> bool:
        self.calls.append(("has_collection", collection, kwargs))
        return self.exists

    def describe_collection(self, collection: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_collection", collection, kwargs))
        return {"fields": [{"name": field} for field in self.fields]}


def _store(client: _SchemaClient) -> NodeFtsStore:
    store = object.__new__(NodeFtsStore)
    store.client = client
    store.collection = "fts-test"
    return store


def test_has_current_schema_passes_timeout_to_collection_probes() -> None:
    client = _SchemaClient(CURRENT_FIELDS)

    assert _store(client).has_current_schema(timeout=2.5) is True
    assert client.calls == [
        ("has_collection", "fts-test", {"timeout": 2.5}),
        ("describe_collection", "fts-test", {"timeout": 2.5}),
    ]


def test_schema_status_distinguishes_missing_collection_without_describing_it() -> None:
    client = _SchemaClient(CURRENT_FIELDS, exists=False)

    assert _store(client).schema_status(timeout=2.5) is CollectionSchemaStatus.MISSING
    assert client.calls == [("has_collection", "fts-test", {"timeout": 2.5})]


def test_has_current_schema_rejects_collection_without_summary_metadata() -> None:
    client = _SchemaClient(
        [field for field in CURRENT_FIELDS if field != "node_summary_truncated"]
    )

    store = _store(client)
    assert store.schema_status() is CollectionSchemaStatus.OUTDATED
    assert store.has_current_schema() is False
