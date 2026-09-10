import pytest

from nianlun.indexing.fts.config import (
    get_fts_analyzer_params,
    get_milvus_language_identifier,
)
from nianlun.indexing.fts.store import query_variants


def test_fts_analyzer_routes_chinese_and_english_with_lingua(monkeypatch):
    monkeypatch.delenv("MILVUS_LANGUAGE_IDENTIFIER", raising=False)
    assert get_fts_analyzer_params() == {
        "tokenizer": {
            "type": "language_identifier",
            "identifier": "lingua",
            "analyzers": {
                "default": {"tokenizer": "standard"},
                "English": {"type": "english"},
                "Chinese": {"tokenizer": "jieba"},
            },
        },
    }


def test_fts_analyzer_uses_mandarin_for_whatlang(monkeypatch):
    monkeypatch.setenv("MILVUS_LANGUAGE_IDENTIFIER", "whatlang")

    assert get_milvus_language_identifier() == "whatlang"
    assert get_fts_analyzer_params()["tokenizer"] == {
        "type": "language_identifier",
        "identifier": "whatlang",
        "analyzers": {
            "default": {"tokenizer": "standard"},
            "English": {"type": "english"},
            "Mandarin": {"tokenizer": "jieba"},
        },
    }


def test_fts_analyzer_rejects_unknown_language_identifier(monkeypatch):
    monkeypatch.setenv("MILVUS_LANGUAGE_IDENTIFIER", "unknown")

    with pytest.raises(ValueError, match="MILVUS_LANGUAGE_IDENTIFIER"):
        get_milvus_language_identifier()


def test_query_variants_cover_english_case_forms():
    assert query_variants("api") == ["api", "API", "Api"]
    assert query_variants("Python") == ["Python", "python", "PYTHON"]
    assert query_variants("营业收入") == ["营业收入"]
