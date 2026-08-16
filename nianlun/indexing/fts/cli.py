"""命令行：重建 FTS 索引。

用法::

    python -m nianlun.indexing.fts.cli --workspace data/workspaces/default
    python -m nianlun.indexing.fts.cli --workspace data/workspaces/default \\
        --uri http://localhost:19530 --collection pageindex_node_fts_18b7abeab1127eeb

共享 collection 时增加 ``--knowledge-base-id``，并在 Agent 的
``KnowledgeBaseConfig.knowledge_base_id`` 中使用同一个值。

总是 drop+recreate 全量重建（无追加模式，auto_id 主键下追加会重复）。
env（.env 或环境变量）：``MILVUS_URI`` / ``MILVUS_TOKEN`` / ``MILVUS_NODE_FTS_COLLECTION``。
"""

from __future__ import annotations

import argparse
import sys

from nianlun.indexing.fts.build import build_node_fts


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m nianlun.indexing.fts.cli")
    ap.add_argument(
        "--workspace",
        required=True,
        help="workspace 目录（含 _meta.json 与 <doc_id>.json）",
    )
    ap.add_argument("--uri", default=None, help="Milvus URI（默认 env MILVUS_URI）")
    ap.add_argument(
        "--token", default=None, help="Milvus token（默认 env MILVUS_TOKEN）"
    )
    ap.add_argument(
        "--collection",
        default=None,
        help="collection 名（默认 env MILVUS_NODE_FTS_COLLECTION）",
    )
    ap.add_argument(
        "--knowledge-base-id",
        default=None,
        help="共享 collection 时的知识库隔离 ID",
    )
    a = ap.parse_args()

    try:
        build_node_fts(
            a.workspace,
            uri=a.uri,
            token=a.token,
            collection_name=a.collection,
            knowledge_base_id=a.knowledge_base_id,
            force=True,
        )
    except Exception as exc:
        print(
            f"[indexing.fts] 建索引失败: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
