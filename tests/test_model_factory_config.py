from __future__ import annotations

import httpx
import pytest

import nianlun.models.embedding as embedding_module
import nianlun.models.llm as llm_module


def _embedding_client(body: str, content_type: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": content_type},
            request=request,
        )

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        event_hooks={
            "response": [
                embedding_module.embedding_response_hook("test-embedding")
            ]
        },
    )


def _embed(body: str, content_type: str = "application/json") -> list[float]:
    client = _embedding_client(body, content_type)
    try:
        model = embedding_module.build_embeddings_model(
            model="test-embedding",
            api_key="test-key",
            base_url="http://test/v1",
            allow_env_fallback=False,
            http_client=client,
        )
        return model.embed_query("连接测试")
    finally:
        client.close()


def test_embedding_factory_normalizes_string_vector_response() -> None:
    assert _embed('"[0.1, 0.2, 0.3]"', "text/plain") == [0.1, 0.2, 0.3]


def test_embedding_factory_normalizes_base64_vector_body() -> None:
    # The OpenAI SDK requests encoding_format=base64 by default; some gateways
    # return the whole vector as a bare base64 float32 string body, which the
    # SDK otherwise crashes on with "'str' object has no attribute 'data'".
    assert _embed('"AAAAAAAAgD8AAABA"') == [0.0, 1.0, 2.0]
    assert _embed("AAAAAAAAgD8AAABA", "text/plain") == [0.0, 1.0, 2.0]


def test_embedding_factory_normalizes_base64_vector_field() -> None:
    assert _embed('{"embedding": "AAAAAAAAgD8AAABA"}') == [0.0, 1.0, 2.0]
    assert _embed('{"data": "AAAAAAAAgD8AAABA"}') == [0.0, 1.0, 2.0]


def test_embedding_factory_normalizes_wrapped_envelope() -> None:
    assert _embed(
        '{"code": 0, "data": {"data": [{"embedding": [0.1, 0.2, 0.3]}]}}'
    ) == [0.1, 0.2, 0.3]


def test_chat_factory_strict_mode_does_not_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_getter() -> str:
        pytest.fail("strict mode must not read the environment")

    monkeypatch.setattr(llm_module, "get_openai_api_key", fail_getter)
    monkeypatch.setattr(llm_module, "get_openai_base_url", fail_getter)
    monkeypatch.setattr(llm_module, "get_openai_model", fail_getter)

    with pytest.raises(RuntimeError, match="未设置 OPENAI_API_KEY"):
        llm_module.build_chat_model(allow_env_fallback=False)


def test_embedding_factory_strict_mode_does_not_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_getter() -> str:
        pytest.fail("strict mode must not read the environment")

    monkeypatch.setattr(embedding_module, "get_openai_api_key", fail_getter)
    monkeypatch.setattr(embedding_module, "get_openai_base_url", fail_getter)
    monkeypatch.setattr(embedding_module, "get_embedding_model", fail_getter)

    with pytest.raises(RuntimeError, match="未设置 OPENAI_API_KEY"):
        embedding_module.build_embeddings_model(allow_env_fallback=False)
