"""仅处理 Markdown 的独立树索引项目。

公共 API：``build_chat_model`` / ``count_tokens``（模型层）、``build_md_index`` /
``build_md_index_sync``（编排入口）。摘要/描述辅助函数在 ``llm`` 子模块内。

依赖：``nianlun.models.llm`` 和顶层公共配置，不直连 Agent 运行时或知识库；与检索侧的
唯一接口是工作区 JSON 文件格式（见 ``docs/architecture/tree_index_design.md`` §7）。
"""

from __future__ import annotations

from nianlun.indexing.tree.llm import build_chat_model, count_tokens
from nianlun.indexing.tree.pipeline import build_md_index, build_md_index_sync

__all__ = [
    "build_chat_model",
    "build_md_index",
    "build_md_index_sync",
    "count_tokens",
]
