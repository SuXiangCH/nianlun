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
