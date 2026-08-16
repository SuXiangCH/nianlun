"""Configuration for the optional dense-vector index."""

from __future__ import annotations

import os

DEFAULT_VECTOR_COLLECTION = "pageindex_doc_vectors_18b7abeab1127eeb"
DEFAULT_EMBEDDING_DIM = 1024
# CJK text can consume about 1.5 tokens per character on common compatible
# tokenizers. Keep each request comfortably below typical 8K-token embedding
# limits without relying on an OpenAI-specific tokenizer at runtime.
VECTOR_NODE_CHAR_LIMIT = 4_000
# Keep semantic node results aligned with FTS post-processing.
VECTOR_NODE_PER_DOC = 5
VECTOR_DERIVE_LIMIT = 512
VECTOR_SEARCH_EF = 64
VECTOR_HNSW_M = 16
VECTOR_HNSW_EF_CONSTRUCTION = 200


def get_vector_collection() -> str:
    """Return the default collection used by the vector CLI/runtime."""
    return os.environ.get("MILVUS_DOC_VECTOR_COLLECTION", DEFAULT_VECTOR_COLLECTION)


def get_vector_enabled() -> bool:
    """Return whether the standalone Agent entry point should expose vector search."""
    raw = os.environ.get("NIANLUN_VECTOR_ENABLED", "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError("NIANLUN_VECTOR_ENABLED 必须是 true/false")


def get_embedding_dim() -> int:
    """Return the configured embedding dimension, validating invalid values early."""
    raw = os.environ.get("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM))
    try:
        dimension = int(raw)
    except ValueError as exc:
        raise ValueError("EMBEDDING_DIM 必须是正整数") from exc
    if dimension <= 0:
        raise ValueError("EMBEDDING_DIM 必须是正整数")
    return dimension


def get_milvus_uri() -> str:
    """Return the Milvus endpoint used by the vector index."""
    return os.environ.get("MILVUS_URI", "http://localhost:19530")


def get_milvus_token() -> str:
    """Return the optional Milvus token."""
    return os.environ.get("MILVUS_TOKEN", "")


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_VECTOR_COLLECTION",
    "VECTOR_HNSW_EF_CONSTRUCTION",
    "VECTOR_HNSW_M",
    "VECTOR_NODE_CHAR_LIMIT",
    "VECTOR_NODE_PER_DOC",
    "VECTOR_DERIVE_LIMIT",
    "VECTOR_SEARCH_EF",
    "get_embedding_dim",
    "get_milvus_token",
    "get_milvus_uri",
    "get_vector_enabled",
    "get_vector_collection",
]
