"""在线知识库的 workspace 和检索配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from nianlun.config import PROJECT_ROOT

WORKSPACE_DIR = Path(
    os.environ.get(
        "NIANLUN_WORKSPACE",
        str(PROJECT_ROOT / "data" / "workspaces" / "default"),
    )
).expanduser()
META_PATH = WORKSPACE_DIR / "_meta.json"
NODE_MATCH_LIMIT = 60
NODE_HINT_SUMMARY_LIMIT = 20


@dataclass(frozen=True)
class KnowledgeBaseConfig:
    """应用绑定的知识库配置。"""

    workspace_dir: Path = WORKSPACE_DIR
    fts_enabled: bool = True
    milvus_uri: str | None = None
    fts_collection: str | None = None
    milvus_token: str | None = None
    knowledge_base_id: str | None = None
    vector_enabled: bool = False
    vector_collection: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None


__all__ = [
    "KnowledgeBaseConfig",
    "META_PATH",
    "NODE_HINT_SUMMARY_LIMIT",
    "NODE_MATCH_LIMIT",
    "WORKSPACE_DIR",
]
