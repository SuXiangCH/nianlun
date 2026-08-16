"""``NodeFtsStore`` 集成测试（需 Milvus standalone，非 Lite）。

BM25 function + analyzer 在 Milvus Lite 不支持，故需 standalone。测试默认跳过，
只有显式设置 ``NIANLUN_RUN_MILVUS_INTEGRATION=1`` 且配置 ``MILVUS_URI`` 时才会
连接 Milvus 并创建临时 collection。
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from nianlun.indexing.fts.build_records import (
    SOURCE_DOC_DESC,
    SOURCE_NODE_TEXT,
    build_records,
)
from nianlun.indexing.fts.config import get_fts_analyzer_params
from nianlun.indexing.fts.store import _PYMILVUS_IMPORT_ERROR, NodeFtsStore

RUN_MILVUS_INTEGRATION = os.environ.get("NIANLUN_RUN_MILVUS_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    _PYMILVUS_IMPORT_ERROR is not None
    or not RUN_MILVUS_INTEGRATION
    or not os.environ.get("MILVUS_URI"),
    reason=(
        "默认不运行真实 Milvus 集成测试；如需运行请同时设置 "
        "NIANLUN_RUN_MILVUS_INTEGRATION=1 和 MILVUS_URI "
        "（指向 Milvus standalone）"
    ),
)


def _store(tmp_path) -> NodeFtsStore:
    """每个用例独立 collection，避免互相污染。"""
    import uuid

    coll = f"fts_test_{uuid.uuid4().hex[:8]}"
    return NodeFtsStore(collection_name=coll)


def _doc() -> dict:
    return {
        "id": "doc-test",
        "doc_name": "伊利测试报告",
        "doc_description": "伊利股份2025年第一季度报告，含主要财务数据、API 与 Python 说明。",
        "line_count": 10,
        "structure": [
            {
                "title": "重要内容提示",
                "node_id": "0001",
                "line_num": 1,
                "text": "重要内容提示：营业收入大幅增长，股东结构稳定。API 与 Python 示例见附录。",
                "summary": "营业收入大幅增长，股东结构稳定。",
            },
        ],
    }


def test_create_collection_idempotent(tmp_path):
    store = _store(tmp_path)
    try:
        store.create_collection()  # 首建
        collection_info: Any = store.client.describe_collection(store.collection)
        text_field = next(
            field
            for field in collection_info["fields"]
            if field["name"] == "text"
        )
        assert json.loads(text_field["params"]["analyzer_params"]) == (
            get_fts_analyzer_params()
        )
        store.create_collection()  # drop+recreate，幂等不报错
    finally:
        if store.client.has_collection(store.collection):
            store.client.drop_collection(store.collection)


def test_insert_and_search_roundtrip(tmp_path):
    store = _store(tmp_path)
    try:
        store.create_collection()
        store.insert(build_records(_doc()))
        store.flush()  # BM25 索引需段封存后才可查（见 store.flush 注释）
        store.load()

        # jieba 分词后"营业收入"应命中（子串扫描会漏，BM25 不漏）
        hits = store.search("营业收入", limit=10)
        assert hits, "应命中含'营业收入'的节点"
        # 命中含输出字段、不含 text
        assert all("text" not in h for h in hits)
        # 至少一条 node_text 命中带 node_id
        node_hits = [h for h in hits if h["source_type"] == SOURCE_NODE_TEXT]
        assert any(h["node_id"] for h in node_hits)
    finally:
        if store.client.has_collection(store.collection):
            store.client.drop_collection(store.collection)


def test_english_search_is_case_insensitive(tmp_path):
    store = _store(tmp_path)
    try:
        store.create_collection()
        store.insert(build_records(_doc()))
        store.flush()
        store.load()

        for query in ("api", "Api", "API", "python", "Python"):
            assert store.search(query, limit=10), f"英文查询 {query!r} 应命中"
    finally:
        if store.client.has_collection(store.collection):
            store.client.drop_collection(store.collection)


def test_doc_desc_hit_only_doc(tmp_path):
    """doc_description 命中 -> doc_desc 记录，node_id 空（只命中文档）。"""
    store = _store(tmp_path)
    try:
        store.create_collection()
        store.insert(build_records(_doc()))
        store.flush()
        store.load()

        hits = store.search("主要财务数据", limit=10)
        # 应有 doc_desc 命中（"主要财务数据"在 doc_description）
        desc_hits = [h for h in hits if h["source_type"] == SOURCE_DOC_DESC]
        assert desc_hits, "应命中 doc_description"
        assert all(h["node_id"] is None for h in desc_hits)
    finally:
        if store.client.has_collection(store.collection):
            store.client.drop_collection(store.collection)


def test_delete_by_doc_removes_only_that_document(tmp_path):
    """``delete_by_doc`` removes one document's records; others are untouched.

    覆盖设计文档 §5.7 的待验证项：delete 后该文档立即不再命中（无需 flush），
    其余文档命中不变。auto_id 主键下按 ``doc_id`` 过滤删除。
    """
    store = _store(tmp_path)
    alpha = {
        "id": "doc-alpha",
        "doc_name": "Alpha 报告",
        "doc_description": "Alpha 文档独有标记 alphatoken 记录",
        "line_count": 1,
        "structure": [
            {
                "title": "Alpha 节点",
                "node_id": "0001",
                "line_num": 1,
                "text": "Alpha 节点正文 alphatoken 内容",
                "summary": "Alpha 摘要",
            },
        ],
    }
    beta = {
        "id": "doc-beta",
        "doc_name": "Beta 报告",
        "doc_description": "Beta 文档独有标记 betatoken 记录",
        "line_count": 1,
        "structure": [
            {
                "title": "Beta 节点",
                "node_id": "0001",
                "line_num": 1,
                "text": "Beta 节点正文 betatoken 内容",
                "summary": "Beta 摘要",
            },
        ],
    }
    try:
        store.create_collection()
        store.insert(build_records(alpha))
        store.insert(build_records(beta))
        store.flush()
        store.load()

        assert {h["doc_id"] for h in store.search("alphatoken", limit=10)} == {
            "doc-alpha"
        }
        assert {h["doc_id"] for h in store.search("betatoken", limit=10)} == {
            "doc-beta"
        }

        store.delete_by_doc("doc-alpha")

        assert store.search("alphatoken", limit=10) == []
        assert {h["doc_id"] for h in store.search("betatoken", limit=10)} == {
            "doc-beta"
        }
    finally:
        if store.client.has_collection(store.collection):
            store.client.drop_collection(store.collection)
