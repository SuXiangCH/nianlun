"""Milvus 全文检索配置（env 读取，自包含）。

集中放置 FTS 模块的运行时常量与 env 取值，便于 CLI / 测试复用。
该配置属于离线 FTS 索引自身，不依赖 Agent 运行时配置。
"""

from __future__ import annotations

import os

# .env 是仓库既有约定（python-dotenv）；缺失时退化为不加载，不报错。
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover

    def load_dotenv(*_args, **_kwargs):
        return False


load_dotenv()

# BM25 原始命中派生上限：search limit 取此值，再分别派生文档摘要和节点候选。
# sparse 不支持 group_by（官方限制），故用较大 derive limit；仅当关键词命中超过
# 此值（无意义词如"的"）时两条召回通道才可能不全，可接受。
DOC_DERIVE_LIMIT = 512

# 节点提示返回上限，与在线知识库的返回上限保持对齐。
NODE_MATCH_LIMIT = 60

# 每文档节点命中上限：防单文档霸榜（top-K 跨文档公平分配配额）；检索后处理用。
NODE_PER_DOC = 3

# 文档摘要通道上限：只限制 doc_desc 命中的候选文档，不限制节点通道召回的文档。
DOC_TOP_N = 20

# Milvus VARCHAR max_length 是【字节】（中文 UTF-8 每字 3 字节），65535 是上限；
# 截断到 60000 留余量，防插入超限。
TEXT_MAX_BYTES = 65535
TEXT_TRUNCATE_BYTES = 60000

# 当前已迁移的默认知识库 collection。API Server 会按知识库生成独立的
# collection；这个值只作为离线 CLI 和 smoke search 的默认目标。
DEFAULT_NODE_FTS_COLLECTION = "pageindex_node_fts_18b7abeab1127eeb"


def get_fts_analyzer_params() -> dict[str, object]:
    """Return the mixed Chinese/English analyzer used by FTS collections.

    ``jieba`` keeps the existing Chinese segmentation behavior, while
    ``lowercase`` makes English terms case-insensitive and
    ``cnalphanumonly`` removes punctuation-only tokens.
    """
    return {
        "tokenizer": {"type": "jieba", "mode": "search"},
        "filter": ["lowercase", "cnalphanumonly"],
    }


def get_milvus_uri() -> str:
    """Milvus 2.6 实例地址。FTS 需 standalone（Lite 不支持 BM25 function），故默认指向 standalone。"""
    return os.environ.get("MILVUS_URI", "http://localhost:19530")


def get_milvus_token() -> str:
    """生产实例 token（可选，本地 standalone 留空）。"""
    return os.environ.get("MILVUS_TOKEN", "")


def get_node_fts_collection() -> str:
    """节点 FTS collection 名。"""
    return os.environ.get("MILVUS_NODE_FTS_COLLECTION", DEFAULT_NODE_FTS_COLLECTION)
