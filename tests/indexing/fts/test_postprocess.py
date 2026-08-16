"""``postprocess`` 单元测试（纯 Python，不需 Milvus）。

覆盖：节点去重（同节点多源合并）、每文档限额（防霸榜）、组合、扁平返回。
数据形态对齐真实测试输出（蓝思科技双命中、无人机霸榜）。
"""

from __future__ import annotations

from nianlun.indexing.fts.build_records import SOURCE_NODE_SUMMARY, SOURCE_NODE_TEXT
from nianlun.indexing.fts.postprocess import (
    cap_per_doc,
    dedup_node_hits,
    postprocess_node_hits,
    top_doc_ids,
)


def _hit(doc_id, node_id, source, score, title="t", line_num=1, doc_name="d"):
    return {
        "doc_id": doc_id,
        "doc_name": doc_name,
        "source_type": source,
        "node_id": node_id,
        "title": title,
        "line_num": line_num,
        "score": score,
    }


# ============ dedup_node_hits ============


def test_dedup_merges_same_node_multi_source():
    """同节点 node_text + node_summary 双命中 -> 合并一条，score 取最高，sources 收集。"""
    hits = [
        _hit("d1", "0002", SOURCE_NODE_SUMMARY, 46.64),
        _hit("d1", "0002", SOURCE_NODE_TEXT, 46.10),
    ]
    out = dedup_node_hits(hits)
    assert len(out) == 1
    assert out[0]["node_id"] == "0002"
    assert out[0]["score"] == 46.64  # max
    assert set(out[0]["matched_sources"]) == {SOURCE_NODE_SUMMARY, SOURCE_NODE_TEXT}


def test_dedup_keeps_distinct_nodes():
    hits = [
        _hit("d1", "0001", SOURCE_NODE_TEXT, 50.0),
        _hit("d1", "0002", SOURCE_NODE_TEXT, 40.0),
    ]
    out = dedup_node_hits(hits)
    assert len(out) == 2
    assert [h["node_id"] for h in out] == ["0001", "0002"]  # score desc


def test_dedup_skips_doc_desc():
    """node_id=None 的 doc_desc 记录不参与节点去重（跳过）。"""
    hits = [
        _hit("d1", None, "doc_desc", 30.0),
        _hit("d1", "0001", SOURCE_NODE_TEXT, 40.0),
    ]
    out = dedup_node_hits(hits)
    assert len(out) == 1
    assert out[0]["node_id"] == "0001"


def test_dedup_same_node_id_different_doc_not_merged():
    """不同文档的同名 node_id 不合并（以 doc_id+node_id 为键）。"""
    hits = [
        _hit("d1", "0001", SOURCE_NODE_TEXT, 40.0),
        _hit("d2", "0001", SOURCE_NODE_TEXT, 50.0),
    ]
    out = dedup_node_hits(hits)
    assert len(out) == 2


def test_dedup_sorted_by_score_desc():
    hits = [
        _hit("d1", "0001", SOURCE_NODE_TEXT, 30.0),
        _hit("d1", "0002", SOURCE_NODE_TEXT, 50.0),
        _hit("d1", "0003", SOURCE_NODE_TEXT, 40.0),
    ]
    out = dedup_node_hits(hits)
    assert [h["score"] for h in out] == [50.0, 40.0, 30.0]


def test_dedup_preserves_flat_structure():
    """去重后仍为扁平 list（不嵌套），每条含原 schema 字段 + matched_sources。"""
    hits = [_hit("d1", "0001", SOURCE_NODE_TEXT, 40.0)]
    out = dedup_node_hits(hits)
    assert isinstance(out, list)
    assert set(out[0]) >= {
        "doc_id",
        "doc_name",
        "node_id",
        "title",
        "line_num",
        "score",
        "matched_sources",
    }


# ============ cap_per_doc ============


def test_cap_limits_single_doc_dominance():
    """单文档 9 个节点（无人机用例形态），cap=3 -> 只留 3 个高分 + 另一文档 1 个。"""
    hits = [_hit("aded", f"{i:04d}", SOURCE_NODE_TEXT, 30.0 - i) for i in range(9)]
    hits.append(_hit("other", "0000", SOURCE_NODE_TEXT, 30.32))
    out = cap_per_doc(hits, per_doc_cap=3)
    aded = [h for h in out if h["doc_id"] == "aded"]
    other = [h for h in out if h["doc_id"] == "other"]
    assert len(aded) == 3
    assert len(other) == 1
    assert [h["score"] for h in aded] == [30.0, 29.0, 28.0]  # 留最高 3 个


def test_cap_multi_doc_independent():
    hits = [
        _hit("d1", "0001", SOURCE_NODE_TEXT, 50.0),
        _hit("d1", "0002", SOURCE_NODE_TEXT, 40.0),
        _hit("d2", "0003", SOURCE_NODE_TEXT, 45.0),
        _hit("d2", "0004", SOURCE_NODE_TEXT, 35.0),
    ]
    out = cap_per_doc(hits, per_doc_cap=1)
    assert {h["node_id"] for h in out} == {"0001", "0003"}  # 各文档最高


def test_cap_preserves_global_score_order():
    hits = [
        _hit("d1", "0001", SOURCE_NODE_TEXT, 50.0),
        _hit("d2", "0002", SOURCE_NODE_TEXT, 45.0),
        _hit("d1", "0003", SOURCE_NODE_TEXT, 40.0),
        _hit("d2", "0004", SOURCE_NODE_TEXT, 35.0),
    ]
    out = cap_per_doc(hits, per_doc_cap=2)
    assert [h["score"] for h in out] == [50.0, 45.0, 40.0, 35.0]


# ============ postprocess_node_hits（组合） ============


def test_postprocess_dedup_then_cap():
    """双命中 + 霸榜：先去重（同节点两源合一），再 cap（单文档限额）。"""
    hits = [
        _hit("d1", "0001", SOURCE_NODE_SUMMARY, 46.64),
        _hit("d1", "0001", SOURCE_NODE_TEXT, 46.10),
        _hit("d1", "0002", SOURCE_NODE_TEXT, 44.98),
        _hit("d2", "0003", SOURCE_NODE_TEXT, 30.32),
    ]
    out = postprocess_node_hits(hits, per_doc_cap=1, limit=10)
    # d1 去重后 2 节点（0001@46.64, 0002@44.98），cap=1 -> 留 0001；d2 留 0003
    assert [h["node_id"] for h in out] == ["0001", "0003"]
    assert out[0]["matched_sources"] == [SOURCE_NODE_SUMMARY, SOURCE_NODE_TEXT]


def test_postprocess_no_cap_only_dedup():
    """per_doc_cap=None -> 仅去重，不限额。"""
    hits = [
        _hit("d1", "0001", SOURCE_NODE_SUMMARY, 46.64),
        _hit("d1", "0001", SOURCE_NODE_TEXT, 46.10),
    ]
    out = postprocess_node_hits(hits)
    assert len(out) == 1
    assert out[0]["score"] == 46.64


def test_postprocess_limit_truncates():
    hits = [_hit("d1", f"{i:04d}", SOURCE_NODE_TEXT, 50.0 - i) for i in range(5)]
    out = postprocess_node_hits(hits, limit=3)
    assert len(out) == 3
    assert [h["score"] for h in out] == [50.0, 49.0, 48.0]


# ============ top_doc_ids（文档级 top-N） ============


def test_top_doc_ids_by_max_score():
    """文档分取该文档所有命中的最高分，按分降序取 top-N。"""
    hits = [
        _hit("d1", "0001", SOURCE_NODE_TEXT, 50.0),
        _hit("d1", "0002", SOURCE_NODE_TEXT, 40.0),  # d1 最高 50
        _hit("d2", "0003", SOURCE_NODE_TEXT, 45.0),  # d2 最高 45
        _hit("d3", None, "doc_desc", 30.0),  # d3 最高 30（doc_desc）
    ]
    assert top_doc_ids(hits, doc_top_n=2) == ["d1", "d2"]


def test_top_doc_ids_none_no_limit():
    hits = [
        _hit("d1", "0001", SOURCE_NODE_TEXT, 50.0),
        _hit("d2", "0002", SOURCE_NODE_TEXT, 45.0),
    ]
    assert top_doc_ids(hits, doc_top_n=None) == ["d1", "d2"]


def test_top_doc_ids_uses_max_per_doc():
    """文档分 = 该文档所有命中记录的最高分（含 doc_desc 与 node 级）。"""
    hits = [
        _hit("d1", "0001", SOURCE_NODE_TEXT, 30.0),
        _hit("d1", "0002", SOURCE_NODE_SUMMARY, 60.0),  # d1 最高 60
        _hit("d2", "0003", SOURCE_NODE_TEXT, 50.0),  # d2 最高 50
    ]
    assert top_doc_ids(hits, doc_top_n=1) == ["d1"]


def test_top_doc_ids_skips_missing_doc_id():
    hits = [
        _hit("d1", "0001", SOURCE_NODE_TEXT, 50.0),
        {
            "doc_id": None,
            "node_id": "x",
            "score": 40.0,
            "source_type": SOURCE_NODE_TEXT,
        },
    ]
    assert top_doc_ids(hits, doc_top_n=10) == ["d1"]


def test_top_doc_ids_default_caps_at_n():
    hits = [_hit(f"d{i}", "0001", SOURCE_NODE_TEXT, 50.0 - i) for i in range(15)]
    out = top_doc_ids(hits, doc_top_n=10)
    assert len(out) == 10
    assert out[0] == "d0"  # 最高分
