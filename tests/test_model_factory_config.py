from __future__ import annotations

import httpx
import pytest

import nianlun.config as config_module
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
            "response": [embedding_module.embedding_response_hook("test-embedding")]
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


def test_llm_factory_wraps_shared_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, dict[str, object]]] = []

    class FakeChatModel:
        model_name = "resolved-model"
        openai_api_base = "https://provider.example/v1"

        async def ainvoke(self, prompt: str) -> str:
            del prompt
            return "{}"

    def fake_build_chat_model(
        model: str | None = None, **kwargs: object
    ) -> FakeChatModel:
        calls.append((model, kwargs))
        return FakeChatModel()

    monkeypatch.setattr(llm_module, "build_chat_model", fake_build_chat_model)

    structured = llm_module.build_llm(
        "judge-model",
        temperature=0.1,
        enable_thinking=True,
        api_key="test-key",
        base_url="https://provider.example/v1",
        allow_env_fallback=False,
    )

    assert calls == [
        (
            "judge-model",
            {
                "temperature": 0.1,
                "enable_thinking": None,
                "api_key": "test-key",
                "base_url": "https://provider.example/v1",
                "allow_env_fallback": False,
            },
        )
    ]
    assert structured.metadata.provider == "openai-compatible"
    assert structured.metadata.model == "resolved-model"
    assert structured.metadata.temperature == 0.1
    assert structured.metadata.enable_thinking is None
    assert structured.metadata.endpoint_identity.startswith("sha256:")
    assert "provider.example" not in structured.metadata.endpoint_identity


def test_chat_factory_only_sends_explicit_thinking_disable() -> None:
    provider_default = llm_module.build_chat_model(
        model="test-model",
        api_key="test-key",
        base_url="https://provider.example/v1",
        allow_env_fallback=False,
    )
    explicit_enable = llm_module.build_chat_model(
        model="test-model",
        enable_thinking=True,
        api_key="test-key",
        base_url="https://provider.example/v1",
        allow_env_fallback=False,
    )
    disabled = llm_module.build_chat_model(
        model="test-model",
        enable_thinking=False,
        api_key="test-key",
        base_url="https://provider.example/v1",
        allow_env_fallback=False,
    )

    assert provider_default.extra_body is None
    assert explicit_enable.extra_body is None
    assert disabled.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("true", None), ("false", False), ("invalid", None)],
)
def test_thinking_override_defaults_to_provider_behavior(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool | None
) -> None:
    if raw is None:
        monkeypatch.delenv("OPENAI_ENABLE_THINKING", raising=False)
    else:
        monkeypatch.setenv("OPENAI_ENABLE_THINKING", raw)

    assert config_module.get_enable_thinking() is expected


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
