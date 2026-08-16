"""Agentic RAG 顶层包：离线索引 + 检索/生成运行时。

本包 ``__init__`` 刻意保持**轻量**：不做 eager re-export，避免 ``import nianlun.indexing.tree``
这类索引侧导入拖起 agent 运行时与知识库实例化。

子包按职责分离：

- :mod:`nianlun.indexing.tree` markdown -> 树索引 JSON（离线索引）
- :mod:`nianlun.indexing.fts`  节点级全文索引构建
- :mod:`nianlun.knowledgebase` 在线知识库访问与检索
- :mod:`nianlun.agent`        交互/批量入口；主 Agent 位于 ``nianlun.agent.lead_agent``
- :mod:`nianlun.models.llm`       共享 LLM 工厂 ``build_chat_model`` 与 content 归一化 ``content_to_text``
- :mod:`nianlun.models.embedding` 共享 Embedding 工厂 ``build_embeddings_model``

典型用法（按需从子模块导入，不再经顶层 re-export）::

    from nianlun.agent.lead_agent.factory import AgentRuntimeFactory
    from nianlun.indexing.tree import build_md_index

    runtime = AgentRuntimeFactory().create()
    result = runtime.invoke("问题")
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    # 版本号唯一事实来源是 pyproject.toml 的 project.version；
    # 源码直跑（未经安装）时兜底为同一值。
    __version__ = _package_version("nianlun")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.1.0"

__all__ = ["__version__"]
