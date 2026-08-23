"""pipeline 单测：覆盖 golden 未触及的纯算法函数（thin_tree / clean_tree_for_output /
create_clean_structure_for_description / structure_to_list / write_node_id）。

golden 用 if_thinning=False 冻结，故 thin_tree 需独立单测。其余函数做最小 sanity。

用法::

    PYTHONPATH=<root> /opt/miniconda3/bin/python3 <root>/tests/indexing/tree/test_pipeline_unit.py
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

from nianlun.indexing.fts.build_records import (
    SOURCE_NODE_TEXT as FTS_NODE_TEXT,
    build_records as build_fts_records,
)
from nianlun.indexing.tree.pipeline import (
    build_tree,
    build_md_index_sync,
    clean_tree_for_output,
    compute_token_counts,
    create_clean_structure_for_description,
    format_structure,
    slice_node_text,
    structure_to_list,
    thin_tree,
    write_node_id,
)
from nianlun.indexing.vector.build_records import (
    SOURCE_NODE_TEXT as VECTOR_NODE_TEXT,
    build_records as build_vector_records,
)


def _make_nodes():
    """合成一棵树：
    # A   (L1)        text="A 开头"        少量
      ## A1 (L2)      text="A1 内容"
      ## A2 (L2)      text="A2 内容"
    # B   (L1)        text="B 内容"
    """
    md = "# A\nA 开头\n## A1\nA1 内容\n## A2\nA2 内容\n# B\nB 内容"
    lines = md.split("\n")
    headings = [
        {"title": "A", "level": 1, "line_num": 1},
        {"title": "A1", "level": 2, "line_num": 3},
        {"title": "A2", "level": 2, "line_num": 5},
        {"title": "B", "level": 1, "line_num": 7},
    ]
    return slice_node_text(headings, lines), md


def test_slice_and_build():
    nodes, _ = _make_nodes()
    assert nodes[0]["text"].startswith("# A"), nodes[0]["text"]
    assert nodes[1]["text"].startswith("## A1"), nodes[1]["text"]
    tree = build_tree(nodes)
    assert tree[0]["title"] == "A" and tree[1]["title"] == "B"
    assert [c["title"] for c in tree[0]["nodes"]] == ["A1", "A2"]
    assert tree[0]["node_id"] == "0001"  # build_tree 先分配 0001 起的临时 id
    write_node_id(tree)
    assert tree[0]["node_id"] == "0000" and tree[0]["nodes"][0]["node_id"] == "0001"


def test_slice_untitled_document():
    lines = ["第一段正文", "第二段正文"]
    nodes = slice_node_text([], lines)
    assert len(nodes) == 1
    assert nodes[0]["title"] == "无标题"
    assert nodes[0]["line_num"] == 1
    assert nodes[0]["text"] == "第一段正文\n第二段正文"


def test_slice_untitled_long_document_is_chunked():
    lines = ["a" * 5_000, "b" * 5_000]
    nodes = slice_node_text([], lines)
    assert len(nodes) > 1
    assert all(node["title"] == "无标题" for node in nodes)
    assert all(len(node["text"]) <= 4_000 for node in nodes)
    assert "".join(node["text"] for node in nodes) == "\n".join(lines).strip()


def test_slice_empty_untitled_document():
    assert slice_node_text([], ["", "  "]) == []


class _StubPlannerLLM:
    """无标题规划器 mock：classify 恒 single_topic；bridge 按 ``merge`` 决定合并或保留边界。"""

    def __init__(self, merge: bool = False):
        self.merge = merge

    async def ainvoke(self, prompt, **_kwargs):
        if "候选原子块：" in prompt:
            raise AssertionError("测试不应触发 boundary 规划")
        if "左规划块 ID：" in prompt:
            left = re.search(r"左规划块 ID：(pc-\d+)", prompt).group(1)
            right = re.search(r"右规划块 ID：(pc-\d+)", prompt).group(1)
            if self.merge:
                return {
                    "left_planning_chunk_id": left,
                    "right_planning_chunk_id": right,
                    "boundary": False,
                    "title": "合并主题",
                    "title_basis_block_ids": re.findall(r'"block_id":"(b\d+)"', prompt)[:1],
                    "confidence": 0.9,
                }
            return {
                "left_planning_chunk_id": left,
                "right_planning_chunk_id": right,
                "boundary": True,
                "confidence": 0.9,
            }
        if "输入章节" in prompt:
            return {"groups": []}
        chunk_id = re.search(r"规划块 ID：(pc-\d+)", prompt).group(1)
        return {
            "planning_chunk_id": chunk_id,
            "classification": "single_topic",
            "title": f"语义标题-{chunk_id}",
            "title_basis_block_ids": re.findall(r'"block_id":"(b\d+)"', prompt)[:1],
            "confidence": 0.9,
        }


def test_build_md_index_untitled_document_preserves_text():
    content = "没有标题的正文。\n" + ("这是很长的一行。" * 1_500)
    with tempfile.TemporaryDirectory() as temp_dir:
        md_path = Path(temp_dir) / "untitled.md"
        md_path.write_text(content, encoding="utf-8")
        result = build_md_index_sync(
            str(md_path),
            llm=_StubPlannerLLM(merge=True),
            add_node_summary=False,
            add_node_text=True,
        )

    structure = result["structure"]
    assert len(structure) == 1
    assert structure[0]["title"] != "无标题"
    assert "没有标题的正文。" in structure[0]["text"]
    assert "这是很长的一行。" in structure[0]["text"]
    assert structure[0]["text"] == content


def _raise_no_api_key(model=None):
    raise RuntimeError("未设置 OPENAI_API_KEY")


def test_build_md_index_untitled_falls_back_without_llm(monkeypatch):
    """LLM 未配置时降级为规则兜底树，不再整篇构建失败。"""
    from nianlun.indexing.tree import pipeline

    monkeypatch.setattr(pipeline, "build_chat_model", _raise_no_api_key)
    content = "没有标题的正文。"
    with tempfile.TemporaryDirectory() as temp_dir:
        md_path = Path(temp_dir) / "untitled.md"
        md_path.write_text(content, encoding="utf-8")
        result = build_md_index_sync(
            str(md_path),
            llm=None,
            model="__missing_model__",
            add_node_summary=False,
            add_node_text=True,
        )
    structure = result["structure"]
    assert len(structure) == 1
    assert "文档内容" in structure[0]["title"]  # 规则兜底标题，不伪装 LLM 语义
    assert structure[0]["text"] == content


def test_build_md_index_untitled_summary_degrades_without_llm(monkeypatch):
    """无标题 + add_node_summary + LLM 未配置：摘要降级（短节点取原文），不整篇失败。"""
    from nianlun.indexing.tree import pipeline

    monkeypatch.setattr(pipeline, "build_chat_model", _raise_no_api_key)
    content = "没有标题的正文。"
    with tempfile.TemporaryDirectory() as temp_dir:
        md_path = Path(temp_dir) / "untitled.md"
        md_path.write_text(content, encoding="utf-8")
        result = build_md_index_sync(
            str(md_path),
            llm=None,
            model="__missing_model__",
            add_node_summary=True,
            add_doc_description=True,
        )
    node = result["structure"][0]
    assert node["summary"] == content  # 低于阈值直接取原文
    assert result["doc_description"] == ""  # LLM 不可用，对齐 describe_document 回落


def test_node_summaries_limit_concurrent_model_calls(monkeypatch):
    from nianlun.indexing.tree import pipeline

    active = 0
    peak = 0

    async def fake_summarize(_llm, _text):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return "summary"

    structure = [
        {"text": "long", "nodes": []}
        for _ in range(pipeline._SUMMARY_CONCURRENCY + 3)  # pyright: ignore[reportPrivateUsage]
    ]
    monkeypatch.setattr(pipeline, "summarize_node", fake_summarize)

    asyncio.run(pipeline._generate_summaries(structure, object(), None, 0))  # pyright: ignore[reportPrivateUsage]

    assert peak == pipeline._SUMMARY_CONCURRENCY  # pyright: ignore[reportPrivateUsage]


def test_untitled_parent_node_text_uses_own_content_only():
    """父节点 text 仅含 own_content_spans，不重复包含子节点正文。"""
    from nianlun.indexing.tree.pipeline import _untitled_tree_to_legacy_structure
    from nianlun.indexing.tree.untitled.models import SectionPlan
    from nianlun.indexing.tree.untitled.validation import assign_own_content_spans

    raw = "引言段。\n\n第一部分正文。\n\n第二部分正文。"
    i1 = raw.index("第一部分正文。")
    i2 = raw.index("第二部分正文。")
    child1 = SectionPlan(
        "s0001", "第一部分", start_char=i1, end_char=i1 + 7, start_line=3, end_line=3
    )
    child2 = SectionPlan(
        "s0002", "第二部分", start_char=i2, end_char=i2 + 7, start_line=5, end_line=5
    )
    parent = SectionPlan(
        "s0003",
        "全文",
        start_char=0,
        end_char=len(raw),
        start_line=1,
        end_line=5,
        child_ids=("s0001", "s0002"),
        depth=2,
    )
    sections = assign_own_content_spans([child1, child2, parent], raw)
    tree = _untitled_tree_to_legacy_structure(tuple(sections), raw)

    assert tree[0]["title"] == "全文"
    assert tree[0]["text"] == "引言段。"
    assert tree[0]["nodes"][0]["text"] == "第一部分正文。"
    assert tree[0]["nodes"][1]["text"] == "第二部分正文。"


def test_pseudo_titled_document_reroutes_to_semantic_planner():
    """仅一个标题 + 超大正文 -> 重路由到语义规划器，不再产出超大截断节点。"""
    content = "# 唯一标题\n" + "word " * 9_000
    with tempfile.TemporaryDirectory() as temp_dir:
        md_path = Path(temp_dir) / "pseudo.md"
        md_path.write_text(content, encoding="utf-8")
        result = build_md_index_sync(
            str(md_path),
            llm=_StubPlannerLLM(),
            add_node_summary=False,
            add_node_text=True,
        )
    titles = [node["title"] for node in result["structure"]]
    assert titles and all(title.startswith("语义标题-") for title in titles)
    assert "唯一标题" not in titles


def test_pseudo_titled_detection_disabled_keeps_titled_path():
    """max_titled_node_tokens=None 时恢复纯二元检测，走有标题算法路径。"""
    content = "# 唯一标题\n" + "word " * 9_000
    with tempfile.TemporaryDirectory() as temp_dir:
        md_path = Path(temp_dir) / "pseudo.md"
        md_path.write_text(content, encoding="utf-8")
        result = build_md_index_sync(
            str(md_path),
            add_node_summary=False,
            add_node_text=True,
            max_titled_node_tokens=None,
        )
    assert result["structure"][0]["title"] == "唯一标题"


def test_normal_titled_document_stays_on_algorithm_path():
    content = "# 标题\n简短正文。"
    with tempfile.TemporaryDirectory() as temp_dir:
        md_path = Path(temp_dir) / "titled.md"
        md_path.write_text(content, encoding="utf-8")
        result = build_md_index_sync(
            str(md_path), add_node_summary=False, add_node_text=True
        )
    assert result["structure"][0]["title"] == "标题"


def test_untitled_chunks_are_kept_by_search_indexes():
    lines = ["第一段内容。" * 900, "第二段内容。" * 900]
    tree = build_tree(slice_node_text([], lines))
    write_node_id(tree)
    doc = {"id": "untitled", "doc_name": "untitled.md", "structure": tree}

    fts_records = build_fts_records(doc)
    fts_texts = [
        record["text"]
        for record in fts_records
        if record["source_type"] == FTS_NODE_TEXT
    ]
    vector_records = build_vector_records(doc)
    vector_texts = [
        record["embed_text"]
        for record in vector_records
        if record["source_type"] == VECTOR_NODE_TEXT
    ]

    expected_texts = [node["text"] for node in tree]
    assert fts_texts == expected_texts
    assert vector_texts == expected_texts


def test_write_node_id_dfs():
    nodes, _ = _make_nodes()
    tree = build_tree(nodes)
    write_node_id(tree)
    ids = [n["node_id"] for n in structure_to_list(tree)]
    assert ids == ["0000", "0001", "0002", "0003"], ids


def test_compute_token_counts_subtree():
    nodes, _ = _make_nodes()
    compute_token_counts(nodes, model=None)
    # 父 A 的子树计数应 >= 自身 + A1 + A2 的自身计数之和
    a, a1, a2, b = nodes
    # 直接断言：A 的 text_token_count（子树）严格大于 A1 自身
    assert a["text_token_count"] > _own_count(a1["text"])
    assert b["text_token_count"] == _own_count(b["text"])  # B 无子，子树=自身


def _own_count(text: str) -> int:
    from nianlun.indexing.tree.llm import count_tokens

    return count_tokens(text, model=None)


def test_thin_tree_merges_small_parent():
    nodes, _ = _make_nodes()
    compute_token_counts(nodes, model=None)
    a_count = nodes[0]["text_token_count"]
    # 阈值略高于 A 的子树计数 -> A 子树被判定过小，子节点并入 A、删除
    result = thin_tree([dict(n) for n in nodes], min_node_token=a_count + 1, model=None)
    titles = [n["title"] for n in result]
    assert titles == ["A", "B"], titles  # A1/A2 被合并删除
    a = result[0]
    assert "A1 内容" in a["text"] and "A2 内容" in a["text"]
    assert "nodes" not in a or not a.get(
        "nodes"
    )  # thin_tree 作用于扁平 list，无 nodes 字段


def test_thin_tree_keeps_large_parent():
    nodes, _ = _make_nodes()
    compute_token_counts(nodes, model=None)
    # 阈值=1（远小于任意子树）-> 不合并，节点全保留
    result = thin_tree([dict(n) for n in nodes], min_node_token=1, model=None)
    assert [n["title"] for n in result] == ["A", "A1", "A2", "B"]


def test_clean_tree_for_output():
    nodes, _ = _make_nodes()
    tree = build_tree(nodes)
    write_node_id(tree)
    cleaned = clean_tree_for_output(tree)
    a = cleaned[0]
    assert set(a.keys()) == {"title", "node_id", "text", "line_num", "nodes"}
    assert set(a["nodes"][0].keys()) == {
        "title",
        "node_id",
        "text",
        "line_num",
    }  # 叶子无 nodes


def test_create_clean_structure_for_description():
    nodes, _ = _make_nodes()
    tree = build_tree(nodes)
    write_node_id(tree)
    # 注入 summary / text / 多余键
    for n in structure_to_list(tree):
        n["summary"] = "S"
        n["text"] = "T"
        n["extra"] = "X"
    cleaned = create_clean_structure_for_description(tree)
    a = cleaned[0]
    assert set(a.keys()) == {"title", "node_id", "summary", "nodes"}, a.keys()
    assert "text" not in a and "extra" not in a


def test_structure_to_list_order():
    nodes, _ = _make_nodes()
    tree = build_tree(nodes)
    flat = structure_to_list(tree)
    assert [n["title"] for n in flat] == ["A", "A1", "A2", "B"]


def test_format_structure_drops_empty_nodes_and_orders():
    nodes, _ = _make_nodes()
    tree = build_tree(nodes)
    write_node_id(tree)
    tree = format_structure(
        tree, order=["title", "node_id", "line_num", "text", "nodes"]
    )
    a1 = tree[0]["nodes"][0]
    assert "nodes" not in a1  # 叶子 nodes 被删
    assert list(a1.keys()) == ["title", "node_id", "line_num", "text"]


TESTS = [
    test_slice_and_build,
    test_slice_untitled_document,
    test_slice_untitled_long_document_is_chunked,
    test_slice_empty_untitled_document,
    test_build_md_index_untitled_document_preserves_text,
    test_untitled_chunks_are_kept_by_search_indexes,
    test_write_node_id_dfs,
    test_compute_token_counts_subtree,
    test_thin_tree_merges_small_parent,
    test_thin_tree_keeps_large_parent,
    test_clean_tree_for_output,
    test_create_clean_structure_for_description,
    test_structure_to_list_order,
    test_format_structure_drops_empty_nodes_and_orders,
]


def main() -> int:
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(
        f"\n{'PASS' if failed == 0 else 'FAIL'}: {len(TESTS) - failed}/{len(TESTS)} unit tests"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())


def test_ensure_llm_disables_thinking_for_reproducible_indexing(monkeypatch):
    """索引默认构建的 LLM 必须显式关闭思考（回归：provider 默认思考会吃空 content）。"""
    from nianlun.indexing.tree import pipeline

    captured = {}

    def fake_build_chat_model(model=None, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(pipeline, "build_chat_model", fake_build_chat_model)
    pipeline._ensure_llm(None, "some-model")

    assert captured.get("enable_thinking") is False
