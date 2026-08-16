"""Milvus vector integration tests; disabled unless explicitly opted in."""

from __future__ import annotations

import os
import uuid

import pytest

from nianlun.indexing.vector.store import _PYMILVUS_IMPORT_ERROR, DocVectorStore

RUN_MILVUS_INTEGRATION = os.environ.get("NIANLUN_RUN_MILVUS_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    _PYMILVUS_IMPORT_ERROR is not None
    or not RUN_MILVUS_INTEGRATION
    or not os.environ.get("MILVUS_URI"),
    reason=(
        "默认不运行真实 Milvus 集成测试；如需运行请同时设置 "
        "NIANLUN_RUN_MILVUS_INTEGRATION=1 和 MILVUS_URI"
    ),
)


def test_vector_grouping_search_returns_one_hit_per_document():
    store = DocVectorStore(
        collection_name=f"vector_test_{uuid.uuid4().hex[:8]}",
        dimension=3,
        knowledge_base_id="kb-1",
    )
    staging_collection = f"{store.collection}_staging"
    try:
        store.create_collection()
        store.insert(
            [
                {
                    "doc_id": "doc-1",
                    "doc_name": "第一份报告",
                    "source_type": "node_text",
                    "knowledge_base_id": "kb-1",
                    "node_id": "0001",
                    "title": "主题一",
                    "line_num": 1,
                    "vector": [1.0, 0.0, 0.0],
                },
                {
                    "doc_id": "doc-1",
                    "doc_name": "第一份报告",
                    "source_type": "node_summary",
                    "knowledge_base_id": "kb-1",
                    "node_id": "0002",
                    "title": "主题二",
                    "line_num": 2,
                    "vector": [0.99, 0.01, 0.0],
                },
                {
                    "doc_id": "doc-2",
                    "doc_name": "第二份报告",
                    "source_type": "doc_desc",
                    "knowledge_base_id": "kb-2",
                    "node_id": None,
                    "title": None,
                    "line_num": None,
                    "vector": [0.0, 1.0, 0.0],
                },
            ]
        )
        store.flush()
        store.load()

        staging = DocVectorStore(
            collection_name=staging_collection,
            dimension=3,
            knowledge_base_id="kb-1",
        )
        staging.create_collection()
        staging.insert(
            [
                {
                    "doc_id": "doc-1",
                    "doc_name": "第一份报告",
                    "source_type": "node_text",
                    "knowledge_base_id": "kb-1",
                    "node_id": "0001",
                    "title": "主题一",
                    "line_num": 1,
                    "vector": [1.0, 0.0, 0.0],
                }
            ]
        )
        staging.flush()
        staging.load()
        store.publish_collection(staging_collection)
        store.validate_collection()
        hits = store.search([1.0, 0.0, 0.0], limit=5)

        assert {hit["doc_id"] for hit in hits} == {"doc-1"}
        assert len([hit for hit in hits if hit["doc_id"] == "doc-1"]) == 1
        assert hits[0]["knowledge_base_id"] == "kb-1"
    finally:
        if store.client.has_collection(store.collection):
            store.client.drop_collection(store.collection)
        if store.client.has_collection(staging_collection):
            store.client.drop_collection(staging_collection)


def test_delete_by_doc_removes_only_that_document():
    """``delete_by_doc`` removes one document's vectors; others are untouched.

    覆盖设计文档 §5.7 的待验证项：delete 后该文档立即不再命中（无需 flush）。
    """
    store = DocVectorStore(
        collection_name=f"vector_test_{uuid.uuid4().hex[:8]}",
        dimension=3,
        knowledge_base_id="kb-1",
    )
    try:
        store.create_collection()
        store.insert(
            [
                {
                    "doc_id": "doc-1",
                    "doc_name": "第一份报告",
                    "source_type": "node_text",
                    "knowledge_base_id": "kb-1",
                    "node_id": "0001",
                    "title": "主题一",
                    "line_num": 1,
                    "vector": [1.0, 0.0, 0.0],
                },
                {
                    "doc_id": "doc-2",
                    "doc_name": "第二份报告",
                    "source_type": "node_text",
                    "knowledge_base_id": "kb-1",
                    "node_id": "0001",
                    "title": "主题二",
                    "line_num": 1,
                    "vector": [0.0, 1.0, 0.0],
                },
            ]
        )
        store.flush()
        store.load()

        assert {hit["doc_id"] for hit in store.search([1.0, 0.0, 0.0], limit=5)} == {
            "doc-1"
        }

        store.delete_by_doc("doc-1")

        assert store.search([1.0, 0.0, 0.0], limit=5) == []
        assert {hit["doc_id"] for hit in store.search([0.0, 1.0, 0.0], limit=5)} == {
            "doc-2"
        }
    finally:
        if store.client.has_collection(store.collection):
            store.client.drop_collection(store.collection)
