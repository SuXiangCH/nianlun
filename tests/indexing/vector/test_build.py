from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import nianlun.indexing.vector.build as build_module


class FakeEmbedder:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding failed")
        return [[1.0, 0.0] for _ in texts]


class FakeClient:
    collections: set[str] = {"vector-target"}

    def has_collection(self, name: str) -> bool:
        return name in self.collections

    def drop_collection(self, name: str) -> None:
        self.collections.discard(name)

    def delete(self, *, collection_name: str, filter: str) -> None:
        # 增量路径 delete_by_doc 走这里；记录被删的 collection 以便断言。
        FakeStore.deleted_collections.append(collection_name)


class FakeStore:
    client = FakeClient()
    published = False
    deleted_docs: list[str] = []
    deleted_collections: list[str] = []

    def __init__(self, *, collection_name: str | None = None, **_: Any) -> None:
        self.collection = collection_name or "vector-target"
        self.client = FakeStore.client

    def create_collection(self) -> None:
        self.client.collections.add(self.collection)

    def ensure_collection(self) -> bool:
        if self.client.has_collection(self.collection):
            return True
        self.create_collection()
        return False

    def delete_by_doc(self, doc_id: str) -> None:
        FakeStore.deleted_docs.append(doc_id)
        self.client.delete(
            collection_name=self.collection, filter=f"doc_id == {doc_id}"
        )

    def insert(self, records: list[dict[str, Any]]) -> None:
        del records

    def flush(self) -> None:
        pass

    def load(self) -> None:
        pass

    def publish_collection(self, staging_collection: str) -> None:
        assert self.client.has_collection(staging_collection)
        self.client.collections.discard(self.collection)
        self.client.collections.discard(staging_collection)
        self.client.collections.add(self.collection)
        FakeStore.published = True


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "_meta.json").write_text(json.dumps({"doc-1": {}}), encoding="utf-8")
    (workspace / "doc-1.json").write_text(
        json.dumps(
            {
                "id": "doc-1",
                "doc_description": "主题",
                "structure": [],
            }
        ),
        encoding="utf-8",
    )
    return workspace


def test_build_publishes_only_after_embedding_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeStore.client.collections = {"vector-target"}
    FakeStore.published = False
    monkeypatch.setattr(build_module, "DocVectorStore", FakeStore)

    result = build_module.build_doc_vectors(
        _workspace(tmp_path),
        collection_name="vector-target",
        embedding_model="test-model",
        embedding_dim=2,
        embedder=FakeEmbedder(),
    )

    assert result.collection == "vector-target"
    assert FakeStore.published is True
    assert FakeStore.client.collections == {"vector-target"}


def test_build_failure_keeps_existing_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeStore.client.collections = {"vector-target"}
    FakeStore.published = False
    monkeypatch.setattr(build_module, "DocVectorStore", FakeStore)

    with pytest.raises(RuntimeError, match="embedding failed"):
        build_module.build_doc_vectors(
            _workspace(tmp_path),
            collection_name="vector-target",
            embedding_model="test-model",
            embedding_dim=2,
            embedder=FakeEmbedder(fail=True),
        )

    assert FakeStore.published is False
    assert FakeStore.client.collections == {"vector-target"}


def _workspace_multi(tmp_path: Path, doc_ids: list[str]) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    meta = {doc_id: {} for doc_id in doc_ids}
    (workspace / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    for doc_id in doc_ids:
        (workspace / f"{doc_id}.json").write_text(
            json.dumps(
                {
                    "id": doc_id,
                    "doc_description": f"主题 {doc_id}",
                    "structure": [],
                }
            ),
            encoding="utf-8",
        )
    return workspace


def test_incremental_operates_on_live_collection_without_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """增量路径：直接操作线上 collection，不走 staging/publish，每文档先 delete_by_doc。"""
    workspace = _workspace_multi(tmp_path, ["doc-1", "doc-2"])
    FakeStore.client.collections = {"vector-target"}
    FakeStore.published = False
    FakeStore.deleted_docs = []
    FakeStore.deleted_collections = []
    monkeypatch.setattr(build_module, "DocVectorStore", FakeStore)

    result = build_module.build_doc_vectors(
        workspace,
        collection_name="vector-target",
        embedding_model="test-model",
        embedding_dim=2,
        embedder=FakeEmbedder(),
        doc_ids=["doc-2"],
        force=False,
    )

    assert result.collection == "vector-target"
    # 增量路径不发布（无蓝绿）。
    assert FakeStore.published is False
    # 仅 doc-2 被定向删除（旧向量）。
    assert FakeStore.deleted_docs == ["doc-2"]
    # delete 走线上 collection。
    assert FakeStore.deleted_collections == ["vector-target"]
    # 线上 collection 保留（未被 drop）。
    assert "vector-target" in FakeStore.client.collections


def test_incremental_embedding_failure_keeps_existing_document_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace_multi(tmp_path, ["doc-1"])
    FakeStore.client.collections = {"vector-target"}
    FakeStore.deleted_docs = []
    monkeypatch.setattr(build_module, "DocVectorStore", FakeStore)

    with pytest.raises(RuntimeError, match="embedding failed"):
        build_module.build_doc_vectors(
            workspace,
            collection_name="vector-target",
            embedding_model="test-model",
            embedding_dim=2,
            embedder=FakeEmbedder(fail=True),
            doc_ids=["doc-1"],
            force=False,
        )

    assert FakeStore.deleted_docs == []


def test_incremental_creates_collection_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """collection 缺失时 ensure_collection 建表后增量写入，仍不走 staging。"""
    workspace = _workspace_multi(tmp_path, ["doc-1", "doc-2"])
    FakeStore.client.collections = set()  # collection 不存在
    FakeStore.published = False
    FakeStore.deleted_docs = []
    FakeStore.deleted_collections = []
    monkeypatch.setattr(build_module, "DocVectorStore", FakeStore)

    build_module.build_doc_vectors(
        workspace,
        collection_name="vector-target",
        embedding_model="test-model",
        embedding_dim=2,
        embedder=FakeEmbedder(),
        doc_ids=["doc-1", "doc-2"],
        force=False,
    )

    assert FakeStore.published is False
    assert "vector-target" in FakeStore.client.collections  # ensure_collection 建表
    assert sorted(FakeStore.deleted_docs) == ["doc-1", "doc-2"]


def test_force_uses_staging_and_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """force 路径走全量蓝绿：staging + publish，不调用 delete_by_doc。"""
    workspace = _workspace_multi(tmp_path, ["doc-1", "doc-2"])
    FakeStore.client.collections = {"vector-target"}
    FakeStore.published = False
    FakeStore.deleted_docs = []
    monkeypatch.setattr(build_module, "DocVectorStore", FakeStore)

    build_module.build_doc_vectors(
        workspace,
        collection_name="vector-target",
        embedding_model="test-model",
        embedding_dim=2,
        embedder=FakeEmbedder(),
        doc_ids=["doc-1", "doc-2"],
        force=True,
    )

    assert FakeStore.published is True
    assert FakeStore.deleted_docs == []  # force 走 drop+recreate，无需 delete_by_doc
