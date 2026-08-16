"""树索引命令行：单文档索引 / 重建工作区。

用法::

    # 单文档索引（打印或落盘）
    python -m nianlun.indexing.tree.cli <doc.md> [-o out.json] [--no-summary]
    # 重建工作区（逐份建索引并写入 <workspace>/<doc_id>.json + _meta.json）
    python -m nianlun.indexing.tree.cli --reindex --workspace <dir> [--no-summary] <md> [<md>...]

工作区格式（docs/architecture/tree_index_design.md §7 契约）::

    <workspace>/<doc_id>.json = {id, type:"md", path(绝对), doc_name, doc_description, line_count, structure}
    <workspace>/_meta.json    = {doc_id: {type, doc_name, doc_description, path, line_count}}

``--no-summary``：纯结构索引（不生成摘要/描述，零 LLM 调用），用于离线快速重建 / 格式 smoke。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nianlun.indexing.tree.pipeline import build_md_index_sync
from nianlun.indexing.tree.workspace import build_workspace_doc, write_workspace_doc


def cmd_single(
    md: str,
    out: str | None,
    model: str | None,
    no_summary: bool,
    full_commonmark: bool = False,
) -> None:
    result = build_md_index_sync(
        md,
        model=model,
        add_node_summary=not no_summary,
        add_doc_description=not no_summary,
        add_node_text=True,
        add_node_id=True,
        atx_only=not full_commonmark,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(
            f"wrote {out}: doc_name={result['doc_name']} "
            f"line_count={result['line_count']} top_nodes={len(result['structure'])}"
        )
    else:
        print(text)


def cmd_reindex(
    workspace: str,
    paths: list[str],
    model: str | None,
    no_summary: bool,
    clean: bool = False,
    full_commonmark: bool = False,
) -> None:
    ws = Path(workspace).expanduser()
    ws.mkdir(parents=True, exist_ok=True)
    if clean:
        stale = list(ws.glob("*.json"))
        for f in stale:
            f.unlink()
        if stale:
            print(f"  cleaned {len(stale)} stale json file(s) in {ws}")
    n = 0
    for md in paths:
        doc_id, doc = build_workspace_doc(
            md, model=model, no_summary=no_summary, atx_only=not full_commonmark
        )
        write_workspace_doc(ws, doc_id, doc)
        n += 1
        print(
            f"  indexed {doc['doc_name']} -> {doc_id}.json "
            f"(line_count={doc['line_count']} top_nodes={len(doc['structure'])})"
        )
    print(f"reindexed {n} docs into {ws}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m nianlun.indexing.tree.cli")
    ap.add_argument(
        "paths",
        nargs="*",
        help="md 路径（单个=索引打印/落盘；--reindex 时为待重建列表）",
    )
    ap.add_argument("--reindex", action="store_true", help="重建工作区模式")
    ap.add_argument("--workspace", default=None, help="重建工作区目录")
    ap.add_argument("-o", "--out", default=None, help="单文档模式输出 JSON 路径")
    ap.add_argument("--model", default=None, help="模型（默认 OPENAI_MODEL）")
    ap.add_argument(
        "--no-summary",
        action="store_true",
        help="纯结构索引（不生成摘要/描述，零 LLM）",
    )
    ap.add_argument(
        "--clean",
        action="store_true",
        help="--reindex 重建前清空 workspace 下既有 *.json（避免新旧并存）",
    )
    ap.add_argument(
        "--full-commonmark",
        action="store_true",
        help="关闭 ATX_ONLY，启用 CommonMark 完整标题语义（setext 等）",
    )
    a = ap.parse_args()
    if a.reindex:
        if not a.workspace or not a.paths:
            ap.error("--reindex 需要 --workspace <dir> 与至少一个 md 路径")
        cmd_reindex(
            a.workspace,
            a.paths,
            a.model,
            a.no_summary,
            clean=a.clean,
            full_commonmark=a.full_commonmark,
        )
    elif a.paths:
        cmd_single(
            a.paths[0], a.out, a.model, a.no_summary, full_commonmark=a.full_commonmark
        )
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
