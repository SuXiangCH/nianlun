"""Build dense-vector records from a workspace document."""

from __future__ import annotations

from typing import Any

from nianlun.indexing.vector.config import VECTOR_NODE_CHAR_LIMIT

SOURCE_DOC_DESC = "doc_desc"
SOURCE_NODE_TEXT = "node_text"
SOURCE_NODE_SUMMARY = "node_summary"


def _walk_nodes(nodes: list[dict[str, Any]]):
    for node in nodes:
        yield node
        children = node.get("nodes")
        if children:
            yield from _walk_nodes(children)


def _summary_field(node: dict[str, Any]) -> str:
    if node.get("nodes"):
        return str(node.get("prefix_summary") or "")
    return str(node.get("summary") or "")


def _is_duplicate_summary(summary: str, text: str) -> bool:
    normalized_summary = " ".join(summary.split())
    normalized_text = " ".join(text.split())
    return bool(normalized_summary) and (
        normalized_summary == normalized_text or normalized_summary in normalized_text
    )


def _truncate_chars(text: str, max_chars: int) -> str:
    """Apply a cheap character limit before sending text to the embedder."""
    if not text or max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def build_records(
    doc: dict[str, Any],
    *,
    node_char_limit: int = VECTOR_NODE_CHAR_LIMIT,
    knowledge_base_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build vectorization inputs for each semantic source.

    ``doc_name`` stays metadata rather than embedding input so entity names do not
    dominate every node vector. This function does not know which embedding model
    will be used and does not produce storage-ready records; pass its output to
    :func:`nianlun.models.embedding.embed_records` first.
    """
    doc_id = str(doc.get("doc_id") or doc.get("id") or "")
    doc_name = str(doc.get("doc_name") or "")
    metadata = {"knowledge_base_id": knowledge_base_id}
    records: list[dict[str, Any]] = []

    description = str(doc.get("doc_description") or "")
    if description.strip():
        records.append(
            {
                **metadata,
                "doc_id": doc_id,
                "doc_name": doc_name,
                "source_type": SOURCE_DOC_DESC,
                "node_id": None,
                "title": None,
                "line_num": None,
                "embed_text": description,
            }
        )

    for node in _walk_nodes(doc.get("structure", [])):
        node_id = node.get("node_id")
        title = str(node.get("title") or "")
        line_num = node.get("line_num")
        text = str(node.get("text") or "")
        summary = _summary_field(node)

        if text.strip():
            records.append(
                {
                    **metadata,
                    "doc_id": doc_id,
                    "doc_name": doc_name,
                    "source_type": SOURCE_NODE_TEXT,
                    "node_id": node_id,
                    "title": title,
                    "line_num": line_num,
                    "embed_text": _truncate_chars(text, node_char_limit),
                }
            )
        if summary.strip() and not _is_duplicate_summary(summary, text):
            records.append(
                {
                    **metadata,
                    "doc_id": doc_id,
                    "doc_name": doc_name,
                    "source_type": SOURCE_NODE_SUMMARY,
                    "node_id": node_id,
                    "title": title,
                    "line_num": line_num,
                    "embed_text": _truncate_chars(summary, node_char_limit),
                }
            )
    return records


__all__ = [
    "SOURCE_DOC_DESC",
    "SOURCE_NODE_SUMMARY",
    "SOURCE_NODE_TEXT",
    "build_records",
]
