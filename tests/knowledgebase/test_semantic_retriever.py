from __future__ import annotations

from nianlun.knowledgebase.semantic_retriever import SemanticDocumentRetriever


class FakeEmbedder:
    def embed_query(self, query: str) -> list[float]:
        assert query == "经营风险"
        return [1.0]


class FakeStore:
    def search(self, vector: list[float], limit: int):
        assert vector == [1.0]
        assert limit == 512
        return [
            {
                "doc_id": "doc-1",
                "doc_name": "第一份",
                "node_id": "1",
                "title": "一",
                "line_num": 1,
                "source_type": "node_text",
                "score": 0.99,
            },
            {
                "doc_id": "doc-1",
                "doc_name": "第一份",
                "node_id": "1",
                "title": "一",
                "line_num": 1,
                "source_type": "node_summary",
                "score": 0.98,
            },
            *[
                {
                    "doc_id": "doc-1",
                    "doc_name": "第一份",
                    "node_id": str(index),
                    "title": str(index),
                    "line_num": index,
                    "source_type": "node_text",
                    "score": 0.97 - index / 100,
                }
                for index in range(2, 8)
            ],
            {
                "doc_id": "doc-2",
                "doc_name": "第二份",
                "node_id": "1",
                "title": "一",
                "line_num": 1,
                "source_type": "node_text",
                "score": 0.96,
            },
        ]


def test_semantic_retriever_deduplicates_nodes_and_caps_each_document():
    result = SemanticDocumentRetriever(FakeStore(), FakeEmbedder()).search(
        "经营风险", limit=2
    )

    assert [item["doc_id"] for item in result] == ["doc-1", "doc-2"]
    assert len(result[0]["node_hints"]) == 5
    assert len(result[1]["node_hints"]) == 1
    assert result[0]["node_hints"][0] == {"node_id": "1", "title": "一", "line_num": 1}
