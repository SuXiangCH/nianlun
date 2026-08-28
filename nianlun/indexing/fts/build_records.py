"""workspace JSON -> 三源 FTS 记录（展平 + 字节截断）。

每文档产出：
- 1 条 ``doc_desc``：``text = doc_description``，``node_id``/``title``/``line_num`` 空（文档级，命中只报文档）。
- 每节点 1 条 ``node_text``：``text = node["text"]``（标题已在 text 首行）。
- 每节点 1 条 ``node_summary``：``text = summary``（叶）/ ``prefix_summary``（非叶）；缺失或与正文重复则跳过（见 :func:`is_dup_summary`）。

``doc_id``/``doc_name`` 每条都带。短节点 summary 常等于正文，去重跳过 ``node_summary``；其余各来源独立成条，无内容重复。
``text`` 按 UTF-8 字节截断到 ``TEXT_TRUNCATE_BYTES``（Milvus VARCHAR max_length 是字节）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from nianlun.indexing.fts.config import (
    NODE_SUMMARY_PREVIEW_CHAR_LIMIT,
    TEXT_TRUNCATE_BYTES,
)

# 三源标识
SOURCE_DOC_DESC = "doc_desc"
SOURCE_NODE_TEXT = "node_text"
SOURCE_NODE_SUMMARY = "node_summary"


def truncate_bytes(text: str, max_bytes: int = TEXT_TRUNCATE_BYTES) -> str:
    """按 UTF-8 字节截断，防切断多字节字符（``errors="ignore"`` 丢弃尾部半截）。

    Milvus VARCHAR ``max_length`` 是字节非字符：中文每字 3 字节，65535 字节≈2.2 万汉字。
    截到 60000 字节留余量，避免插入超限。
    """
    if not text:
        return ""
    b = text.encode("utf-8")
    if len(b) <= max_bytes:
        return text
    return b[:max_bytes].decode("utf-8", errors="ignore")


def walk_nodes(nodes: list[dict]) -> Iterator[dict]:
    """DFS 展平 structure（复制轻量遍历，不 import ``tree_index.pipeline``，保零依赖）。"""
    for node in nodes:
        yield node
        children = node.get("nodes")
        if children:
            yield from walk_nodes(children)


def summary_field(node: dict) -> str:
    """叶子取 ``summary``、非叶取 ``prefix_summary``；缺失空串。

    与 ``tree_index.pipeline._generate_summaries`` 生成语义一致（叶子 summary、非叶 prefix_summary）。
    """
    if node.get("nodes"):  # 非叶
        return node.get("prefix_summary") or ""
    return node.get("summary") or ""


def node_summary_preview(summary: str) -> tuple[str | None, bool | None]:
    """Return compact node navigation metadata and its truncation flag."""
    compact = " ".join(summary.split())
    if not compact:
        return None, None
    truncated = len(compact) > NODE_SUMMARY_PREVIEW_CHAR_LIMIT
    return compact[:NODE_SUMMARY_PREVIEW_CHAR_LIMIT], truncated


def _normalize_ws(s: str) -> str:
    """归一化空白用于重复判定：合并连续空白、去首尾（不影响存入 Milvus 的原文 ``text``）。"""
    return " ".join(s.split())


def is_dup_summary(summary: str, text: str) -> bool:
    """摘要是否与正文重复（重复则跳过 ``node_summary`` 记录）。

    短节点（tree_index 对 token<阈值节点直接用原文当 summary）的 summary 常等于正文或为
    正文去标题的子串 -> 判重复。LLM 浓缩（改写、非逐字子串）不重复 -> 保留双记录。
    用 normalize 后的包含关系判定，不依赖相似度阈值，避免误杀浓缩摘要。
    """
    s = _normalize_ws(summary)
    t = _normalize_ws(text)
    return bool(s) and (s == t or s in t)


def _doc_id_of(doc: dict) -> str:
    """workspace JSON 用 ``id`` 字段（KnowledgeBase.load_doc 重命名为 ``doc_id``）；两者都兼容。"""
    return doc.get("doc_id") or doc.get("id") or ""


def build_records(
    doc: dict,
    *,
    knowledge_base_id: str | None = None,
) -> list[dict[str, Any]]:
    """单文档 -> 三源 FTS 记录列表。

    Args:
        doc: workspace ``<doc_id>.json`` 的 dict（含 ``id``/``doc_name``/``doc_description``/``structure``）。
        knowledge_base_id: 多知识库共用 collection 时写入的隔离字段。

    Returns:
        节点记录同时携带受限 ``node_summary`` 导航元数据；
        ``doc_desc`` 记录的 ``node_id``/``title``/``line_num`` 为 ``None``。
        ``emb_pk``（auto_id）与 ``sparse``（BM25 function 输出）由 Milvus 生成，不含于此。
    """
    doc_id = _doc_id_of(doc)
    doc_name = doc.get("doc_name", "") or ""
    doc_description = doc.get("doc_description", "") or ""
    records: list[dict[str, Any]] = []

    # 1) 文档描述（文档级，命中只报文档不报节点）
    if doc_description.strip():
        records.append(
            {
                "doc_id": doc_id,
                "doc_name": doc_name,
                "source_type": SOURCE_DOC_DESC,
                "node_id": None,
                "title": None,
                "line_num": None,
                "text": truncate_bytes(doc_description),
                "node_summary": None,
                "node_summary_truncated": None,
            }
        )

    # 2)(3) 节点正文 + 节点摘要（节点级，命中带 node_id 可定位节点）
    for node in walk_nodes(doc.get("structure", [])):
        node_id = node.get("node_id")
        title = node.get("title", "") or ""
        line_num = node.get("line_num")
        text = node.get("text", "") or ""
        summary = summary_field(node)
        summary_preview, summary_truncated = node_summary_preview(summary)

        if text.strip():
            records.append(
                {
                    "doc_id": doc_id,
                    "doc_name": doc_name,
                    "source_type": SOURCE_NODE_TEXT,
                    "node_id": node_id,
                    "title": title,
                    "line_num": line_num,
                    "text": truncate_bytes(text),
                    "node_summary": summary_preview,
                    "node_summary_truncated": summary_truncated,
                }
            )
        if summary.strip() and not is_dup_summary(summary, text):
            records.append(
                {
                    "doc_id": doc_id,
                    "doc_name": doc_name,
                    "source_type": SOURCE_NODE_SUMMARY,
                    "node_id": node_id,
                    "title": title,
                    "line_num": line_num,
                    "text": truncate_bytes(summary),
                    "node_summary": summary_preview,
                    "node_summary_truncated": summary_truncated,
                }
            )

    if knowledge_base_id is not None:
        for record in records:
            record["knowledge_base_id"] = knowledge_base_id
    return records
