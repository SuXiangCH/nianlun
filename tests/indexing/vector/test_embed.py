from nianlun.models.embedding import embed_records


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[float(index)] for index, _ in enumerate(texts)]

    def embed_query(self, text):
        return [1.0]


def test_embed_records_preserves_metadata_and_removes_input_text():
    result = embed_records(
        [
            {
                "doc_id": "doc-1",
                "source_type": "doc_desc",
                "embed_text": "主题",
            }
        ],
        FakeEmbedder(),
    )

    assert result == [
        {"doc_id": "doc-1", "source_type": "doc_desc", "vector": [0.0]}
    ]
