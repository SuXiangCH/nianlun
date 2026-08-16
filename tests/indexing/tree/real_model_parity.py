"""真实模型 parity（stage 3）：手动运行，验证 summarize/describe 真实可用。

**不自动跑**（用户要求不自动跑真实模型）：此脚本仅供手动执行。走生产同款流程
（parse → slice → build → write_id → format(带text) → 全量 ``_generate_summaries``
→ clean → describe），检查 ``summary``/``prefix_summary`` 存在且非空、``doc_description`` 非空。
未连接中转站或渠道不可用时 LLM 调用会失败--属预期（stage 0 smoke 未过的信号）。

用法::

    PYTHONPATH=<root> /opt/miniconda3/bin/python3 <root>/tests/indexing/tree/real_model_parity.py [--limit 3]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from nianlun.indexing.tree.llm import build_chat_model, describe_document
from nianlun.indexing.tree.parser import extract_headings
from nianlun.indexing.tree.pipeline import (
    _WITH_TEXT_ORDER,
    _generate_summaries,
    build_tree,
    create_clean_structure_for_description,
    format_structure,
    slice_node_text,
    structure_to_list,
    write_node_id,
)

ROOT = Path(__file__).resolve().parents[3]
DATASETS = ROOT / "data" / "source" / "datasets"
SUMMARY_THRESHOLD = 200  # 与生产 build_md_index 默认一致


async def check_doc(md_path: Path, llm) -> tuple[int, int, bool]:
    md_text = md_path.read_text(encoding="utf-8")
    lines = md_text.split("\n")
    headings = extract_headings(md_text)
    nodes = slice_node_text(headings, lines)
    tree = build_tree(nodes)
    write_node_id(tree)
    tree = format_structure(tree, order=_WITH_TEXT_ORDER)
    # 生产同款：全量摘要（token < 阈值用原文，否则 LLM）
    tree = await _generate_summaries(
        tree, llm, model=None, summary_token_threshold=SUMMARY_THRESHOLD
    )

    flat = structure_to_list(tree)
    total = len(flat)
    nonempty = 0
    for n in flat:
        is_leaf = not n.get("nodes")
        key = "summary" if is_leaf else "prefix_summary"
        if key not in n:
            print(f"  [L{n['line_num']}] MISSING {key}: {n['title'][:40]}")
        elif n[key] and n[key].strip():
            nonempty += 1
        else:
            print(f"  [L{n['line_num']}] EMPTY {key}: {n['title'][:40]}")

    # 生产同款：描述输入为带 summary/prefix_summary 的清洗结构
    clean = create_clean_structure_for_description(tree)
    desc = asyncio.run(describe_document(llm, clean))
    print(f"  doc_description -> {desc[:80]!r}")
    return nonempty, total, bool(desc and desc.strip())


async def main_async(limit: int) -> int:
    llm = build_chat_model()
    mds = sorted(DATASETS.glob("*/full.md"))[:limit]
    print(
        f"checking {len(mds)} docs (model via OPENAI_MODEL/中转站, threshold={SUMMARY_THRESHOLD})"
    )
    ok = total = ok_desc = 0
    for md in mds:
        print(f"\n== {md.parent.name} ==")
        ne, tot, desc_ok = await check_doc(md, llm)
        ok += ne
        total += tot
        ok_desc += int(desc_ok)
    print(
        f"\n=== {ok}/{total} nodes with non-empty summary, "
        f"{ok_desc}/{len(mds)} descriptions non-empty ==="
    )
    return 0 if (ok == total and ok_desc == len(mds) and total > 0) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3, help="检查文档数（默认 3）")
    a = ap.parse_args()
    return asyncio.run(main_async(a.limit))


if __name__ == "__main__":
    raise SystemExit(main())
