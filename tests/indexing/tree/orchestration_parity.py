"""orchestration parity（stage 4）：build_md_index 编排入口。

- 无摘要路径经 build_md_index vs golden structure（硬门禁 0 偏差；``doc_description=None``
  为白名单新键）。
- 带摘要路径用 mock LLM：验证 ``summary``/``prefix_summary``/``doc_description`` 键存在性 +
  text 保留/剥离。
- ``build_md_index_sync`` 与异步结果结构一致。

用法::

    PYTHONPATH=<root> /opt/miniconda3/bin/python3 <root>/tests/indexing/tree/orchestration_parity.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nianlun.indexing.tree.pipeline import (
    build_md_index,
    build_md_index_sync,
    structure_to_list,
)

ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = ROOT / "data" / "source"
GOLDEN_DIR = ROOT / "tests" / "indexing" / "tree" / "golden"


class _Msg:
    def __init__(self, content):
        self.content = content


class MockLLM:
    """返回固定字符串的桩 LLM。"""

    def __init__(self, resp="MOCK_SUMM"):
        self.resp = resp
        self.calls = 0

    async def ainvoke(self, prompt, **kw):
        self.calls += 1
        return _Msg(self.resp)

    def invoke(self, prompt, **kw):
        self.calls += 1
        return _Msg(self.resp)


def main() -> int:
    files = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "_manifest.json")
    print(f"loaded {len(files)} goldens")

    # 1. 无摘要路径经 build_md_index vs golden
    ns_match = ns_mismatch = 0
    for p in files:
        d = json.loads(p.read_text(encoding="utf-8"))
        md_path = str(DATA_ROOT / d["md_path"])
        result = asyncio.run(
            build_md_index(
                md_path, add_node_summary=False, add_node_text=True, add_node_id=True
            )
        )
        g = d["structure"]  # {doc_name, line_count, structure}
        ok = (
            result["doc_name"] == g["doc_name"]
            and result["line_count"] == g["line_count"]
            and result["structure"] == g["structure"]
            and result["doc_description"] is None
        )  # 白名单新键
        if ok:
            ns_match += 1
        else:
            ns_mismatch += 1
            if ns_mismatch <= 3:
                print(
                    f"  MISMATCH {p.stem[:30]}: "
                    f"doc_name={result['doc_name'] == g['doc_name']} "
                    f"line_count={result['line_count'] == g['line_count']} "
                    f"struct={result['structure'] == g['structure']} "
                    f"desc={result['doc_description']}"
                )
    print("\n=== 无摘要路径（build_md_index vs golden）===")
    print(f"  match={ns_match} mismatch={ns_mismatch}")

    # 2. 带摘要路径（mock LLM），取 2 篇
    print("\n=== 带摘要路径（mock LLM，threshold=0 全走 LLM）===")
    mock_fail = 0
    for p in files[:2]:
        d = json.loads(p.read_text(encoding="utf-8"))
        md_path = str(DATA_ROOT / d["md_path"])

        mock = MockLLM("摘要内容")
        r = asyncio.run(
            build_md_index(
                md_path,
                llm=mock,
                add_node_summary=True,
                summary_token_threshold=0,
                add_doc_description=True,
                add_node_text=True,
                add_node_id=True,
            )
        )
        flat = structure_to_list(r["structure"])
        leaves = [n for n in flat if not n.get("nodes")]
        nonleaves = [n for n in flat if n.get("nodes")]
        leaf_no_sum = sum(1 for n in leaves if "summary" not in n)
        nl_no_pre = sum(1 for n in nonleaves if "prefix_summary" not in n)
        text_kept = all("text" in n for n in flat)
        desc_ok = r["doc_description"] == "摘要内容"
        summ_vals = all(n.get("summary") == "摘要内容" for n in leaves)
        ok1 = (
            leaf_no_sum == 0 and nl_no_pre == 0 and text_kept and desc_ok and summ_vals
        )
        if not ok1:
            mock_fail += 1
        print(
            f"  {p.stem[:30]}: {'OK' if ok1 else 'FAIL'} "
            f"leaves={len(leaves)}(no_sum={leaf_no_sum}) "
            f"nonleaves={len(nonleaves)}(no_pre={nl_no_pre}) "
            f"text_kept={text_kept} desc_ok={desc_ok} summ_vals_ok={summ_vals} "
            f"llm_calls={mock.calls}"
        )

        # 无文本变体：摘要后剥离 text，不生成描述
        r2 = asyncio.run(
            build_md_index(
                md_path,
                llm=MockLLM("摘要"),
                add_node_summary=True,
                summary_token_threshold=0,
                add_doc_description=False,
                add_node_text=False,
                add_node_id=True,
            )
        )
        flat2 = structure_to_list(r2["structure"])
        text_stripped = all("text" not in n for n in flat2)
        keys_ok = all(("summary" in n or "prefix_summary" in n) for n in flat2)
        desc_none = r2["doc_description"] is None
        ok2 = text_stripped and keys_ok and desc_none
        if not ok2:
            mock_fail += 1
        print(
            f"  {p.stem[:30]} (no_text): {'OK' if ok2 else 'FAIL'} "
            f"text_stripped={text_stripped} keys_ok={keys_ok} desc_none={desc_none}"
        )

    # 3. build_md_index_sync 一致性
    print("\n=== build_md_index_sync ===")
    p = files[0]
    d = json.loads(p.read_text(encoding="utf-8"))
    md_path = str(DATA_ROOT / d["md_path"])
    r_async = asyncio.run(
        build_md_index(
            md_path,
            llm=MockLLM("S"),
            add_node_summary=True,
            summary_token_threshold=0,
            add_doc_description=True,
            add_node_text=True,
            add_node_id=True,
        )
    )
    r_sync = build_md_index_sync(
        md_path,
        llm=MockLLM("S"),
        add_node_summary=True,
        summary_token_threshold=0,
        add_doc_description=True,
        add_node_text=True,
        add_node_id=True,
    )
    sync_ok = (
        r_async["structure"] == r_sync["structure"]
        and r_async["doc_name"] == r_sync["doc_name"]
    )
    print(
        f"  sync==async: {'OK' if sync_ok else 'FAIL'} "
        f"(nodes={len(structure_to_list(r_sync['structure']))})"
    )

    gate = ns_mismatch == 0 and mock_fail == 0 and sync_ok
    print("\n=== GATE ===")
    print(f"  orchestration parity: {'PASS' if gate else 'REVIEW NEEDED'}")
    return 0 if gate else 2


if __name__ == "__main__":
    raise SystemExit(main())
