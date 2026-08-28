"""``build_records`` 单元测试（纯 Python，不需 Milvus）。

覆盖：字节截断、DFS 展平、summary 字段选择、三源记录正确性、doc_desc 节点字段为空、
summary/doc_description 缺失跳过。
"""

from __future__ import annotations

from nianlun.indexing.fts.build_records import (
    SOURCE_DOC_DESC,
    SOURCE_NODE_SUMMARY,
    SOURCE_NODE_TEXT,
    build_records,
    is_dup_summary,
    node_summary_preview,
    summary_field,
    truncate_bytes,
    walk_nodes,
)
from nianlun.indexing.fts.config import TEXT_TRUNCATE_BYTES

# ============ fixture：模拟一份文档（workspace JSON 形态，doc_id 在 "id" 字段） ============


def _doc() -> dict:
    """根 -> 第一节（非叶，有 prefix_summary）-> 子节点（叶子，有 summary）+ 孤立叶子。"""
    return {
        "id": "doc-1",
        "type": "md",
        "doc_name": "示例文档",
        "doc_description": "这是一份示例文档，涵盖财务与股东内容。",
        "line_count": 30,
        "structure": [
            {
                "title": "第一节 财务",
                "node_id": "0001",
                "line_num": 5,
                "text": "# 第一节 财务\n\n营业收入 100 亿。",
                "prefix_summary": "本节讨论营业收入。",
                "nodes": [
                    {
                        "title": "营业收入明细",
                        "node_id": "0002",
                        "line_num": 8,
                        "text": "营业收入 100 亿。",
                        "summary": "营业收入 100 亿。",
                    },
                ],
            },
            {
                "title": "第二节 股东",
                "node_id": "0003",
                "line_num": 15,
                "text": "# 第二节 股东\n\n股东变动情况。",
                # 无 summary/prefix_summary -> 不产 node_summary 记录
            },
        ],
    }


# ============ truncate_bytes ============


def test_truncate_bytes_short_unchanged():
    assert truncate_bytes("短文本") == "短文本"


def test_truncate_bytes_long_truncated_to_budget():
    text = "营收" * 50000  # 100000 字符 × 3 字节 = 300000 字节，远超 60000
    out = truncate_bytes(text)
    assert len(out.encode("utf-8")) <= TEXT_TRUNCATE_BYTES
    # 不产生乱码（errors="ignore" 丢弃尾部半截多字节字符）
    out.encode("utf-8").decode("utf-8")


def test_truncate_bytes_empty():
    assert truncate_bytes("") == ""


def test_truncate_bytes_custom_budget():
    # 4 中文字 = 12 字节，截到 5 字节 -> 1 个完整字（3 字节）+ 丢弃 2 字节半截
    out = truncate_bytes("营收利润", max_bytes=5)
    assert out == "营"


# ============ walk_nodes（DFS 展平） ============


def test_walk_nodes_dfs_flatten():
    doc = _doc()
    nodes = list(walk_nodes(doc["structure"]))
    assert [n["node_id"] for n in nodes] == ["0001", "0002", "0003"]


def test_walk_nodes_empty():
    assert list(walk_nodes([])) == []


# ============ summary_field ============


def test_summary_field_leaf():
    leaf = {"summary": "叶子摘要", "text": "..."}  # 无 nodes -> 叶子
    assert summary_field(leaf) == "叶子摘要"


def test_summary_field_nonleaf():
    nonleaf = {"prefix_summary": "非叶摘要", "nodes": [{"summary": "子"}]}
    assert summary_field(nonleaf) == "非叶摘要"


def test_summary_field_missing():
    assert summary_field({"text": "x"}) == ""
    assert summary_field({"nodes": []}) == ""


def test_node_summary_preview_normalizes_and_bounds_navigation_metadata():
    preview, truncated = node_summary_preview("  第一行\n\n第二行  ")
    assert preview == "第一行 第二行"
    assert truncated is False

    preview, truncated = node_summary_preview("x" * 301)
    assert preview == "x" * 300
    assert truncated is True


# ============ build_records：三源记录 ============


def test_build_records_three_sources():
    recs = build_records(_doc())
    by_type = {
        t: [r for r in recs if r["source_type"] == t]
        for t in (SOURCE_DOC_DESC, SOURCE_NODE_TEXT, SOURCE_NODE_SUMMARY)
    }

    # 1 条 doc_desc
    assert len(by_type[SOURCE_DOC_DESC]) == 1
    # 3 个节点 -> 3 条 node_text（每个节点都有 text）
    assert len(by_type[SOURCE_NODE_TEXT]) == 3
    # 0001 非叶 prefix_summary（浓缩，非正文子串）-> 1 条；0002 summary==text 重复 -> 跳过；0003 无 -> 共 1 条
    assert len(by_type[SOURCE_NODE_SUMMARY]) == 1


def test_build_records_doc_desc_node_fields_null():
    recs = build_records(_doc())
    desc = next(r for r in recs if r["source_type"] == SOURCE_DOC_DESC)
    assert desc["node_id"] is None
    assert desc["title"] is None
    assert desc["line_num"] is None
    assert desc["doc_id"] == "doc-1"
    assert desc["doc_name"] == "示例文档"
    assert desc["text"] == "这是一份示例文档，涵盖财务与股东内容。"


def test_build_records_can_attach_knowledge_base_id():
    recs = build_records(_doc(), knowledge_base_id="research")
    assert recs
    assert {record["knowledge_base_id"] for record in recs} == {"research"}


def test_build_records_node_text_carries_node_fields():
    recs = build_records(_doc())
    nt = next(
        r
        for r in recs
        if r["source_type"] == SOURCE_NODE_TEXT and r["node_id"] == "0002"
    )
    assert nt["title"] == "营业收入明细"
    assert nt["line_num"] == 8
    assert "营业收入 100 亿" in nt["text"]
    # 摘要记录因与正文重复而不入 BM25，但其导航摘要仍随正文记录持久化。
    assert nt["node_summary"] == "营业收入 100 亿。"
    assert nt["node_summary_truncated"] is False


def test_build_records_node_summary_carries_node_fields():
    recs = build_records(_doc())
    ns = next(
        r
        for r in recs
        if r["source_type"] == SOURCE_NODE_SUMMARY and r["node_id"] == "0001"
    )
    # 0001 非叶 -> prefix_summary
    assert ns["text"] == "本节讨论营业收入。"
    assert ns["node_id"] == "0001"
    assert ns["node_summary"] == "本节讨论营业收入。"


def test_build_records_missing_summary_skipped():
    recs = build_records(_doc())
    # 0003 无 summary/prefix_summary -> 无 node_summary 记录
    assert not [
        r
        for r in recs
        if r["source_type"] == SOURCE_NODE_SUMMARY and r["node_id"] == "0003"
    ]
    # 但其 node_text 仍存在
    assert [
        r
        for r in recs
        if r["source_type"] == SOURCE_NODE_TEXT and r["node_id"] == "0003"
    ]


def test_build_records_missing_doc_description_skipped():
    doc = _doc()
    doc["doc_description"] = ""
    recs = build_records(doc)
    assert not [r for r in recs if r["source_type"] == SOURCE_DOC_DESC]
    # 节点记录照常
    assert [r for r in recs if r["source_type"] == SOURCE_NODE_TEXT]


def test_build_records_accepts_doc_id_field_too():
    """KnowledgeBase.load_doc 把 id 重命名为 doc_id；build_records 两者都兼容。"""
    doc = {
        "doc_id": "kb-1",
        "doc_name": "x",
        "doc_description": "d",
        "structure": [{"title": "t", "node_id": "0001", "line_num": 1, "text": "x"}],
    }
    recs = build_records(doc)
    assert recs[0]["doc_id"] == "kb-1"


# ============ is_dup_summary / 摘要去重 ============


def test_is_dup_summary_equal():
    assert is_dup_summary("营业收入 100 亿。", "营业收入 100 亿。") is True


def test_is_dup_summary_substring():
    # summary 是正文去标题后的子串（短节点原文取摘要的常见形态）
    assert (
        is_dup_summary("营业收入 100 亿。", "# 第一节 财务\n\n营业收入 100 亿。")
        is True
    )


def test_is_dup_summary_concentrated_kept():
    # LLM 浓缩（改写、非逐字子串）-> 不重复，保留双记录
    assert (
        is_dup_summary("本节讨论营业收入。", "# 第一节 财务\n\n营业收入 100 亿。")
        is False
    )


def test_is_dup_summary_empty_summary():
    assert is_dup_summary("", "正文") is False


def test_is_dup_summary_whitespace_only_diff():
    # 空白差异不影响判定（归一化后相同）
    assert is_dup_summary("营收  利润", "营收 利润\n") is True


def test_build_records_dup_summary_skipped():
    """节点 0002 summary==text 重复 -> 不产 node_summary，但 node_text 仍在。"""
    recs = build_records(_doc())
    assert not [
        r
        for r in recs
        if r["source_type"] == SOURCE_NODE_SUMMARY and r["node_id"] == "0002"
    ]
    assert [
        r
        for r in recs
        if r["source_type"] == SOURCE_NODE_TEXT and r["node_id"] == "0002"
    ]


def test_build_records_distinct_summary_kept():
    """节点 0001 prefix_summary 是浓缩（非正文子串）-> 保留 node_summary。"""
    recs = build_records(_doc())
    ns = [
        r
        for r in recs
        if r["source_type"] == SOURCE_NODE_SUMMARY and r["node_id"] == "0001"
    ]
    assert len(ns) == 1
    assert ns[0]["text"] == "本节讨论营业收入。"
