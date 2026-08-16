"""``build_node_fts`` 增量/全量编排单测（无真实 Milvus）。

用 FakeStore 记录 create/ensure/delete/insert/flush/load 调用，验证：
- 增量（``doc_ids``）仅处理指定文档且每文档先 delete_by_doc 后 insert；
- 全量（``force``）drop+recreate 且不调用 delete_by_doc；
- collection 缺失时 ``ensure_collection`` 建表。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import nianlun.indexing.fts.build as build_module


class FakeClient:
    def __init__(self, *, collections: set[str] | None = None) -> None:
        self.collections = set(collections or set())

    def has_collection(self, name: str) -> bool:
        return name in self.collections

    def drop_collection(self, name: str) -> None:
        self.collections.discard(name)


class FakeStore:
    shared_client: FakeClient = FakeClient()
    instances: list["FakeStore"] = []

    def __init__(
        self,
        *,
        uri: str | None = None,
        token: str | None = None,
        collection_name: str | None = None,
        knowledge_base_id: str | None = None,
        **_: Any,
    ) -> None:
        self.collection = collection_name or "fts-target"
        self.knowledge_base_id = knowledge_base_id
        self.client = FakeStore.shared_client
        self.created = False
        self.ensured_existing: bool | None = None
        self.deleted_docs: list[str] = []
        self.inserted: list[dict[str, Any]] = []
        self.flushed = False
        self.loaded = False
        FakeStore.instances.append(self)

    def create_collection(self) -> None:
        self.client.collections.discard(self.collection)
        self.client.collections.add(self.collection)
        self.created = True

    def ensure_collection(self) -> bool:
        if self.client.has_collection(self.collection):
            self.ensured_existing = True
            return True
        self.create_collection()
        self.ensured_existing = False
        return False

    def delete_by_doc(self, doc_id: str) -> None:
        self.deleted_docs.append(doc_id)

    def insert(self, records: list[dict[str, Any]]) -> None:
        self.inserted.extend(records)

    def flush(self) -> None:
        self.flushed = True

    def load(self) -> None:
        self.loaded = True


def _workspace(tmp_path: Path, doc_ids: list[str]) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    meta: dict[str, Any] = {}
    for doc_id in doc_ids:
        meta[doc_id] = {}
        (workspace / f"{doc_id}.json").write_text(
            json.dumps(
                {
                    "id": doc_id,
                    "doc_name": f"{doc_id}.md",
                    "doc_description": f"description {doc_id}",
                    "structure": [],
                }
            ),
            encoding="utf-8",
        )
    (workspace / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return workspace


@pytest.fixture(autouse=True)
def _install_fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_module, "NodeFtsStore", FakeStore)
    FakeStore.instances.clear()
    FakeStore.shared_client = FakeClient()


def _last_store() -> FakeStore:
    assert FakeStore.instances, "build_node_fts 未创建 store"
    return FakeStore.instances[-1]


def test_incremental_processes_only_specified_docs_after_records_are_built(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, ["doc-1", "doc-2", "doc-3"])
    FakeStore.shared_client = FakeClient(collections={"fts-target"})

    build_module.build_node_fts(
        workspace,
        collection_name="fts-target",
        knowledge_base_id="kb-1",
        doc_ids=["doc-2"],
        force=False,
    )

    store = _last_store()
    assert store.created is False
    assert store.ensured_existing is True
    # 仅 doc-2 被定向删除（旧记录）并插入新记录。
    assert store.deleted_docs == ["doc-2"]
    assert len(store.inserted) == 1
    assert {rec["doc_id"] for rec in store.inserted} == {"doc-2"}
    assert store.flushed is True
    assert store.loaded is True


def test_incremental_record_build_failure_keeps_existing_document_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path, ["doc-1"])
    FakeStore.shared_client = FakeClient(collections={"fts-target"})
    monkeypatch.setattr(
        build_module,
        "build_records",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("build failed")),
    )

    with pytest.raises(RuntimeError, match="build failed"):
        build_module.build_node_fts(
            workspace,
            collection_name="fts-target",
            knowledge_base_id="kb-1",
            doc_ids=["doc-1"],
            force=False,
        )

    assert _last_store().deleted_docs == []


def test_force_drops_and_recreates_without_delete(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, ["doc-1", "doc-2"])
    FakeStore.shared_client = FakeClient(collections={"fts-target"})

    build_module.build_node_fts(
        workspace,
        collection_name="fts-target",
        knowledge_base_id="kb-1",
        force=True,
    )

    store = _last_store()
    assert store.created is True
    # force 走 drop+recreate，collection 是空的，无需 delete_by_doc。
    assert store.deleted_docs == []
    assert {rec["doc_id"] for rec in store.inserted} == {"doc-1", "doc-2"}
    assert store.flushed is True


def test_force_rebuild_ignores_partial_doc_ids(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, ["doc-1", "doc-2"])

    build_module.build_node_fts(
        workspace,
        collection_name="fts-target",
        knowledge_base_id="kb-1",
        doc_ids=["doc-1"],
        force=True,
    )

    assert {rec["doc_id"] for rec in _last_store().inserted} == {"doc-1", "doc-2"}


def test_incremental_creates_collection_when_missing(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, ["doc-1", "doc-2"])
    FakeStore.shared_client = FakeClient(collections=set())  # collection 不存在

    build_module.build_node_fts(
        workspace,
        collection_name="fts-target",
        knowledge_base_id="kb-1",
        doc_ids=["doc-1", "doc-2"],
        force=False,
    )

    store = _last_store()
    assert store.ensured_existing is False
    assert store.created is True
    # 非 force 路径仍对每文档 delete_by_doc（对空 collection 是空操作）。
    assert store.deleted_docs == ["doc-1", "doc-2"]
    assert {rec["doc_id"] for rec in store.inserted} == {"doc-1", "doc-2"}


def test_doc_ids_none_processes_all(tmp_path: Path) -> None:
    """CLI 兼容：doc_ids=None 处理 _meta 全部文档。"""
    workspace = _workspace(tmp_path, ["doc-1", "doc-2"])
    FakeStore.shared_client = FakeClient(collections={"fts-target"})

    build_module.build_node_fts(
        workspace,
        collection_name="fts-target",
        knowledge_base_id="kb-1",
        doc_ids=None,
        force=False,
    )

    store = _last_store()
    assert store.ensured_existing is True
    assert sorted(store.deleted_docs) == ["doc-1", "doc-2"]
    assert {rec["doc_id"] for rec in store.inserted} == {"doc-1", "doc-2"}
