"""工作区格式 smoke（stage 5）：用未修改的 KnowledgeBase 加载新模块产出的工作区。

no-summary 重建（零 LLM），验证 docs/architecture/tree_index_design.md §10
工作区格式兼容门禁：
``list_documents`` / ``get_structure_outline`` / ``get_line_content`` 全部正常。

用法::

    PYTHONPATH=<root> /opt/miniconda3/bin/python3 <root>/tests/indexing/tree/workspace_smoke.py
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from nianlun.knowledgebase import KnowledgeBase
from nianlun.indexing.tree.cli import build_workspace_doc, write_workspace_doc

ROOT = Path(__file__).resolve().parents[3]
DATASETS = ROOT / "data" / "source" / "datasets"


def main() -> int:
    mds = sorted(DATASETS.glob("*/full.md"))[:3]
    tmp = Path(tempfile.mkdtemp(prefix="tree_index_ws_"))
    print(f"temp workspace: {tmp}")
    try:
        for md in mds:
            doc_id, doc = build_workspace_doc(str(md), no_summary=True)
            write_workspace_doc(tmp, doc_id, doc)
            print(
                f"  wrote {doc['doc_name']} -> {doc_id}.json "
                f"(line_count={doc['line_count']})"
            )

        # 用未修改的 KnowledgeBase 加载
        kb = KnowledgeBase(workspace_dir=tmp)
        listing = json.loads(kb.list_documents(detailed=True))
        print(f"\nlist_documents: total={listing['total']}")
        assert listing["total"] == len(mds), (
            f"expected {len(mds)} docs, got {listing['total']}"
        )

        for item in listing["documents"]:
            doc_id = item["doc_id"]
            outline = kb.get_structure_outline(doc_id)
            assert outline and "行:" in outline, f"outline empty for {doc_id}"
            m = re.search(r"第 (\d+) 行", outline)
            assert m, f"no line_num in outline for {doc_id}"
            ln = m.group(1)
            content = json.loads(kb.get_line_content(doc_id, ln))
            assert content["matches"] >= 1, (
                f"get_line_content no match for {doc_id} line {ln}"
            )
            text = content["content"][0].get("text", "")
            assert text, f"empty text for {doc_id} line {ln}"
            print(
                f"  {item['doc_name'][:34]}…: outline ok, "
                f"get_line_content(L{ln}) -> {len(text)} chars"
            )

        print("\n=== GATE ===")
        print("  workspace format compatible with unmodified KnowledgeBase: PASS")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print("(cleaned temp workspace)")


if __name__ == "__main__":
    raise SystemExit(main())
