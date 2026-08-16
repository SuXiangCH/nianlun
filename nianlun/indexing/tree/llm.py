"""tree_index 模型层：tiktoken 计数 + 节点摘要 / 文档描述 + 提示词。

ChatOpenAI 构造与 content 文本归一化统一在 ``models.llm``（``build_chat_model`` /
``content_to_text``），本模块从其 re-export。索引侧专属职责：

- ``count_tokens``：tiktoken 直接计数（自定义模型回退 cl100k_base）。与 litellm
  token_counter 行为等价，等价性由 tests/indexing/tree/golden 的冻结计数对比验证
  （docs/architecture/tree_index_design.md §9）。
- ``summarize_node`` / ``describe_document``：单轮补全，失败回落 ``''``，
  单点失败不拖垮 ``asyncio.gather``。
- 节点摘要 / 文档描述提示词：字节级冻结（含空行缩进空格与既有拼写 ``Your are``），
  由 test_llm_unit 的精确字节测试锁定；用显式 ``\\n`` 拼接避免编辑器剥尾随空格。
"""

from __future__ import annotations

import logging
from functools import lru_cache

import tiktoken

from nianlun.models.llm import build_chat_model, content_to_text  # noqa: F401

logger = logging.getLogger(__name__)

__all__ = [
    "build_chat_model",
    "content_to_text",
    "count_tokens",
    "describe_document",
    "summarize_node",
]

# ============ token 计数 ============


@lru_cache(maxsize=1)
def _fallback_encoding():
    """Load the fallback tokenizer only when token counting is requested."""
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str, model: str | None = None) -> int:
    """tiktoken 直接计数。对 OpenAI 官方模型用精确编码，自定义模型回退 cl100k_base。

    与 litellm token_counter 行为等价（litellm 对自定义模型亦回退 cl100k_base）；
    等价性由 tests/indexing/tree/golden 的冻结计数对比验证
    （docs/architecture/tree_index_design.md §9）。
    """
    if not text:
        return 0
    try:
        enc = tiktoken.encoding_for_model(model) if model else _fallback_encoding()
    except KeyError:
        enc = _fallback_encoding()
    return len(enc.encode(text, disallowed_special=()))


# ============ 摘要 / 文档描述 ============


# prompt 为字节级冻结模板；用显式 \n 拼接而非多行 f-string，避免编辑器
# 剥离尾随空格造成静默偏差。
_NODE_SUMMARY_TMPL = (
    "You are given a part of a document, your task is to generate a description of the "
    "partial document about what are main points covered in the partial document.\n"
    "\n"
    "    Partial Document Text: {text}\n"
    "    \n"
    "    Directly return the description, do not include any other text.\n"
    "    "
)

_DOC_DESCRIPTION_TMPL = (
    "Your are an expert in generating descriptions for a document.\n"
    "    You are given a structure of a document. Your task is to generate a one-sentence "
    "description for the document, which makes it easy to distinguish the document from "
    "other documents.\n"
    "        \n"
    "    Document Structure: {structure}\n"
    "    \n"
    "    Directly return the description, do not include any other text.\n"
    "    "
)


def _node_summary_prompt(text: str) -> str:
    """节点摘要 prompt（字节级冻结，含空行缩进空格）。"""
    return _NODE_SUMMARY_TMPL.format(text=text)


def _doc_description_prompt(structure) -> str:
    """文档描述 prompt（字节级冻结，含既有拼写 "Your are"）。"""
    return _DOC_DESCRIPTION_TMPL.format(structure=structure)


async def summarize_node(llm, text: str) -> str:
    """单轮补全，``temperature=0``，直接返回正文。

    失败回落 ``''``：单点失败不拖垮 ``asyncio.gather``。
    """
    if not text:
        return ""
    try:
        resp = await llm.ainvoke(_node_summary_prompt(text), temperature=0)
        return content_to_text(resp)
    except Exception as e:  # pragma: no cover - 失败回落
        logger.warning(
            "[tree_index] summarize_node 失败，回落空串: %s: %s", type(e).__name__, e
        )
        return ""


async def describe_document(llm, structure) -> str:
    """异步单轮，一句话描述。失败回落 ``''``。"""
    try:
        resp = await llm.ainvoke(_doc_description_prompt(structure), temperature=0)
        return content_to_text(resp)
    except Exception as e:  # pragma: no cover - 失败回落
        logger.warning(
            "[tree_index] describe_document 失败，回落空串: %s: %s",
            type(e).__name__,
            e,
        )
        return ""
