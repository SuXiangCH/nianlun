import json

import pytest

from nianlun.knowledgebase import KnowledgeBase
from nianlun.knowledgebase.core import parse_line_spec


def test_parse_line_spec_rejects_excessive_expansion():
    with pytest.raises(ValueError, match="最多允许 500 行"):
        parse_line_spec("1-99999999")

    with pytest.raises(ValueError, match="最多允许 500 行"):
        parse_line_spec(",".join(str(line) for line in range(1, 502)))


def _make_workspace(tmp_path):
    doc_id = "doc-1"
    (tmp_path / "_meta.json").write_text(
        json.dumps({doc_id: {"doc_name": "长文档", "line_count": 1}}),
        encoding="utf-8",
    )
    (tmp_path / f"{doc_id}.json").write_text(
        json.dumps(
            {
                "doc_name": "长文档",
                "line_count": 1,
                "structure": [
                    {
                        "node_id": "0000",
                        "title": "参考文献",
                        "line_num": 1,
                        "text": "0123456789ABCDEFGHIJ",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return KnowledgeBase(tmp_path), doc_id


def test_get_line_content_supports_sliding_windows(tmp_path):
    kb, doc_id = _make_workspace(tmp_path)

    first = json.loads(kb.get_line_content(doc_id, "1", char_offset=5, char_limit=6))
    item = first["content"][0]
    assert item["text"] == "56789A"
    assert item["total_chars"] == 20
    assert item["text_truncated"] is True
    assert item["next_char_offset"] == 11
    assert first["has_more"] is True

    second = json.loads(
        kb.get_line_content(
            doc_id,
            "1",
            char_offset=item["next_char_offset"],
            char_limit=6,
        )
    )
    assert second["content"][0]["text"] == "BCDEFG"
    assert second["content"][0]["next_char_offset"] == 17


def test_get_line_content_without_limit_returns_full_node(tmp_path):
    kb, doc_id = _make_workspace(tmp_path)

    result = json.loads(kb.get_line_content(doc_id, "1"))
    item = result["content"][0]
    assert item["text"] == "0123456789ABCDEFGHIJ"
    assert item["text_truncated"] is False
    assert item["next_char_offset"] is None
    assert result["has_more"] is False


def test_get_line_content_returns_doc_name(tmp_path):
    kb, doc_id = _make_workspace(tmp_path)

    result = json.loads(kb.get_line_content(doc_id, "1"))
    assert result["doc_name"] == "长文档"
    assert result["doc_id"] == doc_id


def test_search_document_nodes_returns_only_bounded_top_hint_summaries(tmp_path):
    metadata = {}
    hits = []
    for doc_index in range(8):
        doc_id = f"doc-{doc_index}"
        metadata[doc_id] = {"doc_name": doc_id, "line_count": 3}
        for node_index in range(3):
            rank = doc_index * 3 + node_index
            node_id = f"node-{rank}"
            summary = "x" * 300 if rank == 0 else f"索引摘要 {rank}"
            hits.append(
                {
                    "doc_id": doc_id,
                    "doc_name": doc_id,
                    "source_type": "node_text",
                    "node_id": node_id,
                    "title": f"章节 {rank}",
                    "line_num": rank + 1,
                    "node_summary": summary,
                    "node_summary_truncated": rank == 0,
                    "score": 100.0 - rank,
                }
            )
    (tmp_path / "_meta.json").write_text(json.dumps(metadata), encoding="utf-8")

    class FakeSearcher:
        def search(self, query, limit, doc_ids=None):
            assert query == "目标概念"
            assert limit == 512
            assert doc_ids is None
            return hits

    kb = KnowledgeBase(tmp_path, full_text_retriever=FakeSearcher())
    result = json.loads(kb.search_document_nodes("目标概念"))
    hints = [
        hint for document in result["documents"] for hint in document["node_hints"]
    ]

    assert len(hints) == 24
    assert hints[0]["summary"] == "x" * 300
    assert hints[0]["summary_truncated"] is True
    assert hints[1]["summary"] == "索引摘要 1"
    assert hints[1]["summary_truncated"] is False
    assert all("summary" in hint for hint in hints[:20])
    assert all("summary" not in hint for hint in hints[20:])
    assert all("summary_truncated" not in hint for hint in hints[20:])
