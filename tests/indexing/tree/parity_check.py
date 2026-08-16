"""parity 检查：新 parser vs 冻结的 golden。

对比维度（docs/architecture/tree_index_design.md §9 阶段 1）：
- ``line_num`` 集合：硬门禁，0 个未解释偏差。
- ``level`` / ``title``：对交集内节点逐项 diff（title 的 ATX 后缀等已知改进项需人工确认）。
- token 等价性：新 ``count_tokens``(tiktoken) vs golden ``litellm_count``。

用法::

    PYTHONPATH=. /opt/miniconda3/bin/python3 tests/indexing/tree/parity_check.py
"""

from __future__ import annotations

import json
from pathlib import Path

from nianlun.indexing.tree.llm import count_tokens
from nianlun.indexing.tree.parser import extract_headings

ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = ROOT / "data" / "source"
GOLDEN_DIR = ROOT / "tests" / "indexing" / "tree" / "golden"


def load_goldens() -> list[tuple[str, dict]]:
    files = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "_manifest.json")
    return [(p.stem, json.loads(p.read_text(encoding="utf-8"))) for p in files]


def main() -> int:
    goldens = load_goldens()
    print(f"loaded {len(goldens)} golden docs")

    agg = dict(
        docs=0,
        golden_nodes=0,
        new_headings=0,
        line_old_only=0,
        line_new_only=0,
        level_diff=0,
        title_diff=0,
        token_match=0,
        token_mismatch=0,
        token_skip=0,
    )
    line_dev: list[tuple[str, str, int, str]] = []
    title_diff_ex: list[tuple] = []
    token_mismatch_ex: list[tuple] = []

    for key, d in goldens:
        agg["docs"] += 1
        md_text = (DATA_ROOT / d["md_path"]).read_text(encoding="utf-8")
        src_lines = md_text.split("\n")

        new_h = extract_headings(md_text, atx_only=True)
        g_nodes = d["nodes"]
        g_by_ln = {n["line_num"]: n for n in g_nodes}
        n_by_ln = {h["line_num"]: h for h in new_h}
        g_lns, n_lns = set(g_by_ln), set(n_by_ln)
        agg["golden_nodes"] += len(g_lns)
        agg["new_headings"] += len(n_lns)

        for ln in sorted(g_lns - n_lns):
            agg["line_old_only"] += 1
            if len(line_dev) < 40:
                src = src_lines[ln - 1][:80] if 0 < ln <= len(src_lines) else "?"
                line_dev.append(("old_only", key, ln, src))
        for ln in sorted(n_lns - g_lns):
            agg["line_new_only"] += 1
            if len(line_dev) < 40:
                src = src_lines[ln - 1][:80] if 0 < ln <= len(src_lines) else "?"
                line_dev.append(("new_only", key, ln, src))

        for ln in sorted(g_lns & n_lns):
            g, n = g_by_ln[ln], n_by_ln[ln]
            if g["level"] != n["level"]:
                agg["level_diff"] += 1
                if len(title_diff_ex) < 30:
                    title_diff_ex.append(("level", key, ln, g["level"], n["level"]))
            if g["title"] != n["title"]:
                agg["title_diff"] += 1
                if len(title_diff_ex) < 30:
                    title_diff_ex.append(
                        ("title", key, ln, g["title"][:40], n["title"][:40])
                    )
            lc = g.get("litellm_count")
            if lc is None:
                agg["token_skip"] += 1
                continue
            nc = count_tokens(g["text"])
            if nc == lc:
                agg["token_match"] += 1
            else:
                agg["token_mismatch"] += 1
                if len(token_mismatch_ex) < 20:
                    token_mismatch_ex.append((key, ln, lc, nc, g["text"][:40]))

    print("\n=== AGGREGATE ===")
    for k, v in agg.items():
        print(f"  {k}: {v}")

    print("\n=== line_num 偏差（硬门禁：需全部可解释）===")
    print(
        f"  old_only(golden有/new无)={agg['line_old_only']}  "
        f"new_only(new有/golden无)={agg['line_new_only']}"
    )
    for kind, key, ln, src in line_dev:
        print(f"  [{kind}] {key[:28]}… L{ln}: {src!r}")

    print("\n=== level/title diff（交集内）===")
    for ex in title_diff_ex:
        print(f"  {ex}")

    print("\n=== token 等价性 ===")
    print(
        f"  match={agg['token_match']} mismatch={agg['token_mismatch']} skip={agg['token_skip']}"
    )
    for key, ln, lc, nc, txt in token_mismatch_ex:
        print(f"  {key[:24]}… L{ln}: litellm={lc} tiktoken={nc} text={txt!r}")

    gate_ok = agg["line_old_only"] == 0 and agg["line_new_only"] == 0
    print("\n=== GATE ===")
    print(f"  line_num 0 偏差硬门禁: {'PASS' if gate_ok else 'REVIEW NEEDED'}")
    return 0 if gate_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
