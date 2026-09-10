from nianlun.agent.lead_agent.routing import maybe_handle_non_retrieval_query
from nianlun.agent.language import prefers_english


def test_direct_english_greeting_has_an_english_reply():
    result = maybe_handle_non_retrieval_query("Hello")

    assert result["route"] == "direct"
    assert result["answer"] == (
        "Hello. You can ask me questions about documents in the knowledge base."
    )


def test_english_question_with_chinese_name_prefers_english():
    assert prefers_english("What did Zhang San say about revenue?")
