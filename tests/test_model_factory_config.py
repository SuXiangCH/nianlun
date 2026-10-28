from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError

import nianlun.config as config_module
import nianlun.models.embedding as embedding_module
import nianlun.models.llm as llm_module


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
@pytest.mark.parametrize("temperature", [None, 0.0, 0.65])
@pytest.mark.parametrize("model_name", ["test-model", "gpt-5-compatible", "gpt-5-pro"])
def test_explicit_temperature_is_independent_of_reasoning_on_wire(
    monkeypatch: pytest.MonkeyPatch,
    effort: str | None,
    temperature: float | None,
    model_name: str,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                json={
                    "id": "resp-test",
                    "object": "response",
                    "created_at": 0,
                    "model": model_name,
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg-test",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "ok", "annotations": []}
                            ],
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    chat_class = llm_module.ChatOpenAI
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            llm_module,
            "ChatOpenAI",
            lambda **kwargs: chat_class(**kwargs, http_client=client),
        )
        model = llm_module.build_chat_model(
            model=model_name,
            api_key="test-key",
            base_url="http://test/v1",
            allow_env_fallback=False,
            temperature=temperature,
            reasoning_effort=effort,  # type: ignore[arg-type]
        )
        assert llm_module.content_to_text(model.invoke("test")) == "ok"

    assert len(requests) == 1
    sent_effort = (
        requests[0].get("reasoning", {}).get("effort")
        if model_name == "gpt-5-pro"
        else requests[0].get("reasoning_effort")
    )
    assert sent_effort == effort
    if temperature is None:
        assert "temperature" not in requests[0]
    else:
        assert requests[0]["temperature"] == temperature


@pytest.mark.parametrize(
    "builder", [llm_module.build_llm, llm_module.build_structured_chat_model]
)
@pytest.mark.parametrize("temperature", [None, 0.0, 0.65])
def test_explicit_temperature_is_retained_in_client_metadata(
    builder, temperature
) -> None:
    client = builder(
        model="test-model",
        api_key="test-key",
        allow_env_fallback=False,
        temperature=temperature,
        reasoning_effort="high",
    )
    assert client.metadata.temperature == temperature


@pytest.mark.parametrize(
    "builder", [llm_module.build_llm, llm_module.build_structured_chat_model]
)
def test_wrapped_factory_preserves_omitted_temperature_for_provider_defaults(builder):
    from nianlun.evaluation import RagEvaluator

    client = builder(
        model="gpt-5-compatible",
        api_key="test-key",
        base_url="http://test/v1",
        allow_env_fallback=False,
    )
    chat_model = client._invoke.__self__
    assert chat_model.temperature is None
    assert chat_model.extra_body is None
    assert client.metadata.temperature is None
    explicit = builder(
        model="gpt-5-compatible",
        api_key="test-key",
        base_url="http://test/v1",
        allow_env_fallback=False,
        temperature=0.0,
    )
    assert (
        RagEvaluator(judge=client).evaluator_fingerprint
        != RagEvaluator(judge=explicit).evaluator_fingerprint
    )


def test_reasoning_clients_with_distinct_temperatures_have_distinct_cache_keys() -> (
    None
):
    from nianlun.evaluation import RagEvaluator

    fingerprints = {
        RagEvaluator(
            judge=llm_module.build_llm(
                model="test-model",
                api_key="test-key",
                allow_env_fallback=False,
                temperature=temperature,
                reasoning_effort="high",
            )
        ).evaluator_fingerprint
        for temperature in (None, 0.0, 0.65)
    }
    assert len(fingerprints) == 3


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_chat_factory_reasoning_wire_payload(
    monkeypatch: pytest.MonkeyPatch, effort: str | None
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "reasoning-alias",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    chat_class = llm_module.ChatOpenAI
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            llm_module,
            "ChatOpenAI",
            lambda **kwargs: chat_class(**kwargs, http_client=client),
        )
        model = llm_module.build_chat_model(
            model="reasoning-alias",
            api_key="test-key",
            base_url="http://test/v1",
            allow_env_fallback=False,
            reasoning_effort=effort,  # type: ignore[arg-type]
        )
        assert model.invoke("test").content == "ok"

    assert len(requests) == 1
    if effort is None:
        assert "reasoning_effort" not in requests[0]
        assert requests[0]["temperature"] == 0.0
    else:
        assert requests[0]["reasoning_effort"] == effort
        assert "temperature" not in requests[0]


def test_reasoning_default_omits_constructor_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_kwargs: dict[str, object] = {}

    def fake_chat_model(**kwargs: object) -> object:
        constructor_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(llm_module, "ChatOpenAI", fake_chat_model)
    llm_module.build_chat_model(
        model="test-model", api_key="test-key", allow_env_fallback=False
    )
    assert "reasoning_effort" not in constructor_kwargs


def test_explicit_reasoning_takes_precedence_over_legacy_thinking_disable() -> None:
    model = llm_module.build_chat_model(
        model="test-model",
        api_key="test-key",
        base_url="http://test/v1",
        allow_env_fallback=False,
        enable_thinking=False,
        reasoning_effort="high",
    )
    assert model.reasoning_effort == "high"
    assert model.extra_body is None


def test_provider_rejection_of_reasoning_is_not_retried_without_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            400,
            json={"error": {"message": "reasoning_effort is unsupported"}},
        )

    chat_class = llm_module.ChatOpenAI
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            llm_module,
            "ChatOpenAI",
            lambda **kwargs: chat_class(**kwargs, http_client=client),
        )
        model = llm_module.build_chat_model(
            model="test-model",
            api_key="test-key",
            base_url="http://test/v1",
            allow_env_fallback=False,
            reasoning_effort="high",
            max_retries=0,
        )
        with pytest.raises(BadRequestError, match="reasoning_effort is unsupported"):
            model.invoke("test")

    assert len(requests) == 1
    assert requests[0]["reasoning_effort"] == "high"


@pytest.mark.parametrize("effort", ["none", "default", "inherit", "HIGH", ""])
def test_chat_factory_rejects_invalid_reasoning_effort(effort: str) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        llm_module.build_chat_model(
            model="test-model",
            api_key="test-key",
            allow_env_fallback=False,
            reasoning_effort=effort,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_structured_factory_reasoning_metadata_and_cache_identity(
    monkeypatch: pytest.MonkeyPatch, effort: str | None
) -> None:
    calls: list[dict[str, object]] = []

    class FakeChatModel:
        model_name = "test-model"
        openai_api_base = "http://test/v1"

        async def ainvoke(self, prompt: str) -> str:
            return prompt

    def fake_chat_model(*args: object, **kwargs: object) -> FakeChatModel:
        calls.append(kwargs)
        return FakeChatModel()

    monkeypatch.setattr(llm_module, "build_chat_model", fake_chat_model)
    client = llm_module.build_structured_chat_model(
        model="test-model",
        api_key="test-key",
        enable_thinking=False,
        allow_env_fallback=False,
        reasoning_effort=effort,  # type: ignore[arg-type]
    )
    assert calls[0].get("reasoning_effort") == effort
    assert ("reasoning_effort" in calls[0]) is (effort is not None)
    assert client.metadata.reasoning_effort == effort
    assert client.metadata.temperature == (0.0 if effort is None else None)
    assert client.metadata.enable_thinking is (False if effort is None else None)
    assert client.fingerprint_config.get("reasoning_effort") == effort


@pytest.mark.parametrize("temperature", [None, 0.0, 0.65])
def test_agent_factory_preserves_explicit_temperature_in_strict_mode(
    monkeypatch: pytest.MonkeyPatch, temperature: float | None
) -> None:
    from nianlun.agent.lead_agent import factory as agent_factory

    class ConstructionStopped(Exception):
        pass

    def capture_model(**kwargs: object) -> None:
        assert kwargs["temperature"] == temperature
        raise ConstructionStopped

    monkeypatch.setattr(agent_factory, "build_chat_model", capture_model)
    with pytest.raises(ConstructionStopped):
        agent_factory.AgentRuntimeFactory(
            model="catalog-model",
            api_key="test-key",
            knowledge_base=SimpleNamespace(has_fts=True),
            allow_env_fallback=False,
            temperature=temperature,
            reasoning_effort="high",
        ).create()


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

    configured = llm_module.build_chat_model(
        model="catalog-model",
        api_key="test-key",
        allow_env_fallback=False,
        reasoning_effort="medium",
    )
    assert configured.model_name == "catalog-model"
    assert configured.reasoning_effort == "medium"


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_agent_factory_passes_reasoning_without_environment_fallback(
    monkeypatch: pytest.MonkeyPatch, effort: str | None
) -> None:
    from nianlun.agent.lead_agent import factory as agent_factory

    class ConstructionStopped(Exception):
        pass

    calls: list[dict[str, object]] = []

    def capture_model(**kwargs: object) -> None:
        calls.append(kwargs)
        raise ConstructionStopped

    def fail_getter():
        pytest.fail("strict mode must not read the environment")

    for name in (
        "get_openai_model",
        "get_openai_api_key",
        "get_openai_base_url",
        "get_openai_temperature",
        "get_enable_thinking",
    ):
        monkeypatch.setattr(agent_factory, name, fail_getter)
    monkeypatch.setattr(agent_factory, "build_chat_model", capture_model)

    with pytest.raises(ConstructionStopped):
        agent_factory.AgentRuntimeFactory(
            model="catalog-model",
            api_key="test-key",
            knowledge_base=SimpleNamespace(has_fts=True),
            allow_env_fallback=False,
            reasoning_effort=effort,  # type: ignore[arg-type]
        ).create()

    assert calls[0].get("reasoning_effort") == effort
    assert ("reasoning_effort" in calls[0]) is (effort is not None)


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


def test_chat_factory_probe_limits_do_not_change_runtime_defaults() -> None:
    defaults = {
        "model": "test-model",
        "api_key": "test-key",
        "allow_env_fallback": False,
    }
    runtime = llm_module.build_chat_model(**defaults)
    probe = llm_module.build_chat_model(
        **defaults, request_timeout_seconds=20.0, max_retries=0
    )
    assert runtime.request_timeout == 300.0
    assert runtime.max_retries == 2
    assert probe.request_timeout == 20.0
    assert probe.max_retries == 0
    assert probe.root_client.max_retries == 0


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
