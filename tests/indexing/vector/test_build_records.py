from nianlun.indexing.vector.build_records import (
    SOURCE_DOC_DESC,
    SOURCE_NODE_SUMMARY,
    SOURCE_NODE_TEXT,
    build_records,
)
from nianlun.indexing.vector.config import VECTOR_NODE_CHAR_LIMIT


def test_default_node_text_budget_is_safe_for_cjk_embedding_models():
    assert VECTOR_NODE_CHAR_LIMIT == 4_000


def test_build_records_emits_three_semantic_sources():
    records = build_records(
        {
            "id": "doc-1",
            "doc_name": "报告",
            "doc_description": "文档主题描述",
            "structure": [
                {
                    "node_id": "0001",
                    "title": "经营情况",
                    "line_num": 10,
                    "text": "营业收入和利润变化。",
                    "summary": "分析营业收入和利润变化趋势。",
                }
            ],
        }
    )

    assert [record["source_type"] for record in records] == [
        SOURCE_DOC_DESC,
        SOURCE_NODE_TEXT,
        SOURCE_NODE_SUMMARY,
    ]
    assert records[1]["node_id"] == "0001"
    assert records[2]["line_num"] == 10


def test_build_records_carries_collection_binding_metadata():
    records = build_records(
        {
            "id": "doc-1",
            "doc_description": "主题",
            "structure": [],
        },
        knowledge_base_id="kb-1",
    )

    assert records == [
        {
            "knowledge_base_id": "kb-1",
            "doc_id": "doc-1",
            "doc_name": "",
            "source_type": SOURCE_DOC_DESC,
            "node_id": None,
            "title": None,
            "line_num": None,
            "embed_text": "主题",
        }
    ]


def test_duplicate_summary_is_not_embedded_twice():
    records = build_records(
        {
            "id": "doc-1",
            "doc_name": "报告",
            "structure": [
                {
                    "node_id": "0001",
                    "line_num": 1,
                    "text": "相同正文",
                    "summary": "相同正文",
                }
            ],
        }
    )

    assert [record["source_type"] for record in records] == [SOURCE_NODE_TEXT]


def test_node_text_is_limited_by_character_budget():
    text = "这是一个很长的节点。" * 100
    records = build_records(
        {
            "id": "doc-1",
            "structure": [
                {
                    "node_id": "0001",
                    "line_num": 1,
                    "text": text,
                }
            ],
        },
        node_char_limit=10,
    )

    assert len(records[0]["embed_text"]) < len(text)
