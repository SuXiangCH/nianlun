"""pipeline parity：新管线（无摘要）vs golden 全量结构 + 子树 token 计数。

docs/architecture/tree_index_design.md §9 阶段 2：算法管线移植后逐项 golden diff。
- 全量结构 diff（含 text / node_id / line_num / title / 嵌套 / 键序）：硬门禁 0 偏差。
- 子树 token 计数 diff（compute_token_counts vs golden subtree_token_counts）：0 偏差。

用法::

    PYTHONPATH=<root> /opt/miniconda3/bin/python3 <root>/tests/indexing/tree/pipeline_parity.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from nianlun.indexing.tree.parser import extract_headings
from nianlun.indexing.tree.pipeline import (
    build_tree,
    compute_token_counts,
    format_structure,
    slice_node_text,
    write_node_id,
)

ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = ROOT / "data" / "source"
GOLDEN_DIR = ROOT / "tests" / "indexing" / "tree" / "golden"
NO_SUMMARY_ORDER = [
    "title",
    "node_id",
    "line_num",
    "summary",
    "prefix_summary",
    "text",
    "nodes",
]


def run_no_summary_pipeline(md_path: Path) -> tuple[dict, list[dict]]:
    md_text = md_path.read_text(encoding="utf-8")
    lines = md_text.split("\n")
    headings = extract_headings(md_text)
    nodes = slice_node_text(headings, lines)
    tree = build_tree(nodes)
    write_node_id(tree)
    tree = format_structure(tree, order=NO_SUMMARY_ORDER)
    result = {
        "doc_name": os.path.splitext(os.path.basename(str(md_path)))[0],
        "line_count": md_text.count("\n") + 1,
        "structure": tree,
    }
    return result, nodes


def diff_structure(a, b, path="root", diffs=None, limit=15):
    if diffs is None:
        diffs = []
    if len(diffs) >= limit:
        return diffs
    if type(a) is not type(b):
        diffs.append(
            (
                path,
                f"type {type(a).__name__} vs {type(b).__name__}",
                str(a)[:50],
                str(b)[:50],
            )
        )
        return diffs
    if isinstance(a, dict):
        ka, kb = set(a), set(b)
        if ka != kb:
            diffs.append(
                (
                    path,
                    f"keys diff {sorted(ka ^ kb)}",
                    f"new-only {sorted(ka - kb)}",
                    f"golden-only {sorted(kb - ka)}",
                )
            )
        for k in a.keys() & b.keys():
            diff_structure(a[k], b[k], f"{path}.{k}", diffs, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            diffs.append((path, f"len {len(a)} vs {len(b)}", "", ""))
        for idx in range(min(len(a), len(b))):
            diff_structure(a[idx], b[idx], f"{path}[{idx}]", diffs, limit)
    else:
        if a != b:
            diffs.append((path, "value", str(a)[:50], str(b)[:50]))
    return diffs


def main() -> int:
    files = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "_manifest.json")
    print(f"loaded {len(files)} goldens")

    struct_match = struct_mismatch = 0
    token_match = token_mismatch = 0
    struct_diff_examples = []
    token_diff_examples = []

    for p in files:
        d = json.loads(p.read_text(encoding="utf-8"))
        md_path = DATA_ROOT / d["md_path"]
        result, nodes = run_no_summary_pipeline(md_path)
        golden_struct = d["structure"]

        if result == golden_struct:
            struct_match += 1
        else:
            struct_mismatch += 1
            if len(struct_diff_examples) < 5:
                diffs = diff_structure(result, golden_struct)
                struct_diff_examples.append((p.stem, diffs))

        # 子树 token 计数
        nodes_copy = [dict(n) for n in nodes]
        compute_token_counts(nodes_copy, model=None)
        subtree = d["subtree_token_counts"]
        for n in nodes_copy:
            ln = n["line_num"]
            # golden 经 JSON 序列化后 dict 键为 str
            gc = subtree.get(str(ln))
            if gc is None:
                continue
            nc = n.get("text_token_count")
            if nc == gc:
                token_match += 1
            else:
                token_mismatch += 1
                if len(token_diff_examples) < 15:
                    token_diff_examples.append((p.stem, ln, gc, nc))

    print("\n=== 全量结构 diff（无摘要管线 vs golden）===")
    print(f"  match={struct_match} mismatch={struct_mismatch}")
    for stem, diffs in struct_diff_examples:
        print(f"  {stem[:30]}…:")
        for d_ in diffs:
            print(f"    {d_}")

    print("\n=== 子树 token 计数 diff（compute_token_counts vs golden）===")
    print(f"  match={token_match} mismatch={token_mismatch}")
    for stem, ln, gc, nc in token_diff_examples:
        print(f"  {stem[:24]}… L{ln}: golden={gc} new={nc}")

    gate = struct_mismatch == 0 and token_mismatch == 0
    print("\n=== GATE ===")
    print(f"  pipeline parity: {'PASS' if gate else 'REVIEW NEEDED'}")
    return 0 if gate else 2


if __name__ == "__main__":
    raise SystemExit(main())
