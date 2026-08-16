"""编排：workspace JSON -> 三源 FTS 记录 -> 攒批 insert -> load。

读 ``_meta.json`` 注册表，逐份加载 ``<doc_id>.json``，``build_records`` 产出三源记录，
攒批插入 Milvus。支持全量（``force=True`` drop+recreate）与增量（``doc_ids`` 定向
delete+insert，设计文档 §5.3）两种模式。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nianlun.indexing.fts.build_records import build_records
from nianlun.indexing.fts.store import NodeFtsStore

logger = logging.getLogger(__name__)

# 攒批插入：减少往返。~8k 记录十几批完事。
BATCH_SIZE = 500


def _load_meta(workspace: Path) -> dict[str, Any]:
    meta_path = workspace / "_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"workspace 缺少 _meta.json: {workspace}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def build_node_fts(
    workspace_dir: str | Path,
    *,
    uri: str | None = None,
    token: str | None = None,
    collection_name: str | None = None,
    knowledge_base_id: str | None = None,
    doc_ids: list[str] | None = None,
    force: bool = False,
) -> NodeFtsStore:
    """重建 FTS 索引：遍历 workspace -> 三源记录 -> insert -> load。

    Args:
        workspace_dir: 含 ``_meta.json`` 与 ``<doc_id>.json`` 的目录。
        uri/token/collection_name: 缺省读 env（见 :mod:`.config`）。
        doc_ids: 仅处理这些文档（增量）。``None`` = 处理 ``_meta.json`` 全部文档
            （离线 CLI 全量兼容）。全量重建时始终处理全部文档。
        force: ``True`` = drop+recreate collection（全量，回收倒排碎片）；``False`` =
            ``ensure_collection``（缺失才建，保留已有记录），并对每个文档先
            ``delete_by_doc`` 再 insert（定向刷新，避免 auto_id 重复）。

    Returns:
        已 load 的 ``NodeFtsStore``，可继续 ``search``。
    """
    ws = Path(workspace_dir)
    meta = _load_meta(ws)
    store = NodeFtsStore(
        uri=uri,
        token=token,
        collection_name=collection_name,
        knowledge_base_id=knowledge_base_id,
    )
    if force:
        store.create_collection()
    else:
        store.ensure_collection()

    # A recreated collection has no records for documents omitted from doc_ids.
    # Treat force as an unambiguous full rebuild instead of silently dropping them.
    target_ids = list(meta.keys()) if force or doc_ids is None else list(doc_ids)

    batch: list[dict[str, Any]] = []
    total = 0
    processed = 0
    for doc_id in target_ids:
        doc_path = ws / f"{doc_id}.json"
        if not doc_path.exists():
            logger.warning(
                "[fts_index] 跳过：%s.json 不存在（_meta 与文件不一致）", doc_id
            )
            continue
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        records = build_records(doc, knowledge_base_id=knowledge_base_id)
        if not force:
            # Read and build first: malformed new data must not erase live results.
            store.delete_by_doc(doc_id)
        for rec in records:
            batch.append(rec)
            if len(batch) >= BATCH_SIZE:
                store.insert(batch)
                total += len(batch)
                batch.clear()
        processed += 1
    if batch:
        store.insert(batch)
        total += len(batch)

    store.flush()  # 封存段：BM25 稀疏索引需段封存后才可查（否则刚插入数据检索不到）
    store.load()
    mode = "增量" if doc_ids is not None and not force else "全量"
    logger.info(
        "[fts_index] 索引构建完成(%s): %s 文档 / %s 记录 -> %s",
        mode,
        processed,
        total,
        store.collection,
    )
    return store
