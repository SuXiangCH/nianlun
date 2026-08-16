"""全文检索（节点级 BM25）离线索引 + 检索模块。

多记录单字段结构（镜像向量设计，非混合搜索）：一个 ``text`` BM25 字段、一个 ``sparse``，
每来源一条记录、``source_type`` 区分--``doc_desc``（1/文档）/``node_text``（1/节点）/
``node_summary``（1/节点，缺失跳过）。任一命中即文档命中。

消费 workspace JSON（``_meta.json`` + ``<doc_id>.json``），产出 Milvus collection。
**不依赖** ``tree_index``；对 ``agent`` 零依赖（config 自包含）。

设计见 ``docs/architecture/fts_design.md``；用法::

    python -m nianlun.indexing.fts.cli --workspace data/workspaces/default
    # 共享 collection 时为每个知识库传独立 ID
    python -m nianlun.indexing.fts.cli --workspace data/workspaces/default \\
        --knowledge-base-id research
"""

from __future__ import annotations
