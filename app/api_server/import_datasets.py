"""把仓库自带的开源数据集批量导入 API Server 知识库。

在仓库根目录执行（建议 API Server 停止或空闲时运行，避免并发写同一工作区）::

    uv run python -m app.api_server.import_datasets --knowledge-base 财报数据集 --create

按 ``datasets/workspace/_meta.json`` 逐篇导入：Markdown 正文与预建标题树
（含 LLM 摘要）一起走正常入库生命周期（sources/ 原文、workspace 树 JSON、
SQLite 文档记录、content_version），**不产生任何模型调用**。重复执行时按
内容哈希幂等跳过已导入文档。导入完成后自动触发 FTS 构建并等待其结束
（FTS 为本地 BM25 构建，同样零模型调用；需要 Milvus 可达）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.api_server.apis.v1.schemas import KnowledgeBaseCreateRequest
from app.api_server.config import get_settings
from app.api_server.services.container import build_services

FTS_WAIT_TIMEOUT_SECONDS = 600


def iter_dataset_documents(datasets_dir: Path) -> Iterator[dict[str, Any]]:
    """Yield ``{doc_id, filename, content, tree}`` for every dataset document."""
    meta_path = datasets_dir / "workspace" / "_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for doc_id in sorted(meta):
        info = meta[doc_id]
        tree_path = datasets_dir / "workspace" / f"{doc_id}.json"
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
        # 树内 path 形如 datasets/<doc-name>/full.md；取目录名回到数据集根下定位。
        doc_dir = Path(str(tree.get("path") or info.get("path") or "")).parent.name
        source = datasets_dir / doc_dir / "full.md"
        if not source.is_file():
            raise FileNotFoundError(f"找不到数据集正文: {source}")
        doc_name = str(info.get("doc_name") or doc_dir)
        filename = f"{Path(doc_name).stem or 'document'}.md"
        yield {
            "doc_id": doc_id,
            "filename": filename,
            "content": source.read_bytes(),
            "tree": tree,
        }


def _find_knowledge_base(services: Any, ref: str) -> Any:
    for item in services.knowledge_bases.list():
        if str(item.id) == ref or item.name == ref:
            return item
    return None


def _wait_for_fts(services: Any, knowledge_base_id: str) -> str:
    deadline = time.monotonic() + FTS_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        item = services.fts.knowledge_base_lookup(knowledge_base_id)
        status = str(item.get("fts_status") or "")
        if status not in {"pending", "building"}:
            return status
        time.sleep(1.0)
    return "timeout"


def main(
    argv: list[str] | None = None, settings: Any | None = None
) -> int:
    parser = argparse.ArgumentParser(
        description="把 datasets/ 的 Markdown 正文与预建树导入 API Server 知识库"
    )
    parser.add_argument("--datasets-dir", default="datasets", help="数据集目录")
    parser.add_argument(
        "--knowledge-base",
        required=True,
        help="目标知识库的 id 或名称（配合 --create 可按名称新建）",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="知识库不存在时按 --knowledge-base 名称新建",
    )
    args = parser.parse_args(argv)

    datasets_dir = Path(args.datasets_dir).resolve()
    if not (datasets_dir / "workspace" / "_meta.json").is_file():
        print(f"数据集清单不存在: {datasets_dir / 'workspace' / '_meta.json'}", file=sys.stderr)
        return 1

    settings = settings or get_settings()
    services = build_services(settings)

    kb = _find_knowledge_base(services, args.knowledge_base)
    if kb is None:
        if not args.create:
            names = ", ".join(item.name for item in services.knowledge_bases.list())
            print(
                f"知识库不存在: {args.knowledge_base}（现有: {names or '无'}；"
                "可加 --create 新建）",
                file=sys.stderr,
            )
            return 1
        kb = services.knowledge_bases.create(
            KnowledgeBaseCreateRequest(name=args.knowledge_base)
        )
        print(f"已创建知识库: {kb.name} ({kb.id})")
    knowledge_base_id = str(kb.id)

    imported = replayed = failed = 0
    total = 0
    for doc in iter_dataset_documents(datasets_dir):
        total += 1
        try:
            response = services.documents.upload(
                knowledge_base_id,
                doc["filename"],
                doc["content"],
                "text/markdown",
                f"import:{doc['doc_id']}",
                prebuilt_document=doc["tree"],
            )
        except Exception as exc:  # 单篇失败不中断整批
            failed += 1
            print(f"  [失败] {doc['filename']}: {exc}", file=sys.stderr)
            continue
        if getattr(response, "idempotent_replay", False):
            replayed += 1
        else:
            imported += 1
        if total % 20 == 0:
            print(f"  进度: {total} 篇（新导入 {imported}，跳过 {replayed}，失败 {failed}）")

    print(f"导入完成: 共 {total} 篇 = 新导入 {imported} + 幂等跳过 {replayed} + 失败 {failed}")

    fts_failed = False
    if settings.fts_enabled:
        print("触发 FTS 构建并等待完成……")
        try:
            services.fts.schedule(knowledge_base_id)
        except Exception as exc:
            print(f"FTS 调度失败: {exc}", file=sys.stderr)
            return 1 if failed else 0
        fts_status = _wait_for_fts(services, knowledge_base_id)
        print(f"FTS 状态: {fts_status}")
        if fts_status != "ready":
            fts_failed = True
            print("FTS 未就绪；可稍后在服务端重试或检查 Milvus 连接", file=sys.stderr)
    else:
        print("FTS 未启用（settings.fts_enabled=False），跳过索引构建")

    return 1 if (failed or fts_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
