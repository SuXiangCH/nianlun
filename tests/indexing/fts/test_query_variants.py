from nianlun.indexing.fts.config import get_fts_analyzer_params
from nianlun.indexing.fts.store import query_variants


def test_fts_analyzer_normalizes_mixed_language_terms():
    assert get_fts_analyzer_params() == {
        "tokenizer": {"type": "jieba", "mode": "search"},
        "filter": ["lowercase", "cnalphanumonly"],
    }


def test_query_variants_cover_english_case_forms():
    assert query_variants("api") == ["api", "API", "Api"]
    assert query_variants("Python") == ["Python", "python", "PYTHON"]
    assert query_variants("营业收入") == ["营业收入"]
