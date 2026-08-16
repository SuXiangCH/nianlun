"""Command line entry point for rebuilding the optional vector index."""

from __future__ import annotations

import argparse
import sys

from nianlun.indexing.vector.build import build_doc_vectors


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m nianlun.indexing.vector.cli"
    )
    parser.add_argument(
        "--workspace", required=True, help="包含 _meta.json 和文档 JSON 的 workspace"
    )
    parser.add_argument("--uri", default=None, help="Milvus URI（默认 env MILVUS_URI）")
    parser.add_argument(
        "--token", default=None, help="Milvus token（默认 env MILVUS_TOKEN）"
    )
    parser.add_argument(
        "--collection",
        default=None,
        help="vector collection（默认 env MILVUS_DOC_VECTOR_COLLECTION）",
    )
    parser.add_argument(
        "--model", default=None, help="embedding 模型（默认 env EMBEDDING_MODEL）"
    )
    parser.add_argument(
        "--dim", type=int, default=None, help="向量维度（默认 env EMBEDDING_DIM）"
    )
    parser.add_argument(
        "--knowledge-base-id",
        default=None,
        help="知识库 ID；共享 collection 时用于隔离检索结果",
    )
    args = parser.parse_args()
    try:
        build_doc_vectors(
            args.workspace,
            uri=args.uri,
            token=args.token,
            collection_name=args.collection,
            embedding_model=args.model,
            embedding_dim=args.dim,
            knowledge_base_id=args.knowledge_base_id,
            force=True,
        )
    except Exception as exc:
        print(
            f"[vector_index] 构建失败: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
