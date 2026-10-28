from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from langchain_openai import ChatOpenAI

import nianlun.models.llm as llm_module
from app.api_server.apis.v1.publishing_schemas import (
    PublishedConfig,
    PublishedLoopGuard,
)
from app.api_server.services.application_service import ApplicationService
from app.api_server.services.model_config_service import ModelConfigService
from nianlun.agent.lead_agent import factory as agent_factory
from nianlun.agent.prompt_registry import CURRENT_PROMPT_BUNDLE
from nianlun.knowledgebase.factory import KnowledgeBaseFactory


@pytest.fixture
def model_transport(monkeypatch):
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                json={
                    "id": "resp-test",
                    "object": "response",
                    "created_at": 0,
                    "model": payload["model"],
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
                "id": "chat-test",
                "object": "chat.completion",
                "created": 0,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(
            llm_module, "ChatOpenAI", partial(ChatOpenAI, http_client=client)
        )
        yield client, requests


def _legacy_sampling(client, requests, model, temperature, effort):
    previous = ChatOpenAI(
        model=model,
        api_key="test-key",
        base_url="http://test/v1",
        temperature=temperature if effort is None else None,
        reasoning_effort=effort,
        http_client=client,
    )
    assert llm_module.content_to_text(previous.invoke("test")) == "ok"
    return requests[-1].get("temperature", "omitted")


@pytest.mark.parametrize("model_name", ["test-model", "gpt-5-compatible", "gpt-5-pro"])
@pytest.mark.parametrize("effort", [None, "high"])
@pytest.mark.parametrize("legacy", [False, True])
def test_enrichment_sampling_preserves_legacy_sdk_behavior(
    model_transport, model_name, effort, legacy
):
    client, requests = model_transport
    expected = _legacy_sampling(client, requests, model_name, 0.0, effort)
    profile = {
        "id": "llm",
        "kind": "llm",
        "name": "test",
        "model": model_name,
        "api_key": "test-key",
        "base_url": "http://test/v1",
        "temperature": 0.65,
        "reasoning_effort": effort,
    }
    service = ModelConfigService(SimpleNamespace(get_model_profile=lambda _id: profile))
    model = service.build_llm("llm", legacy_temperature=legacy)
    assert llm_module.content_to_text(model.invoke("test")) == "ok"
    assert requests[-1].get("temperature", "omitted") == (expected if legacy else 0.65)


@pytest.mark.parametrize("model_name", ["test-model", "gpt-5-compatible", "gpt-5-pro"])
@pytest.mark.parametrize("effort", [None, "high"])
@pytest.mark.parametrize("temperature", [0.0, 0.65])
@pytest.mark.parametrize("sampling_version", [1, 2])
def test_release_sampling_preserves_legacy_sdk_behavior(
    tmp_path,
    monkeypatch,
    model_transport,
    model_name,
    effort,
    temperature,
    sampling_version,
):
    client, requests = model_transport
    expected = _legacy_sampling(client, requests, model_name, temperature, effort)
    knowledge = {
        "id": "kb",
        "workspace_dir": str(tmp_path),
        "content_version": 1,
        "snapshot_relpath": "snapshots/r1",
        "snapshot_manifest_sha256": "a" * 64,
    }
    monkeypatch.setattr(
        KnowledgeBaseFactory,
        "create",
        lambda *args, **kwargs: SimpleNamespace(has_fts=True),
    )

    class ModelConstructed(Exception):
        pass

    def build_and_invoke(**kwargs):
        model = llm_module.build_chat_model(**kwargs)
        assert llm_module.content_to_text(model.invoke("test")) == "ok"
        raise ModelConstructed

    monkeypatch.setattr(agent_factory, "build_chat_model", build_and_invoke)
    service = ApplicationService(
        SimpleNamespace(),
        lambda _id: knowledge,
        model_config_service=SimpleNamespace(
            require_llm_profile=lambda _id: {"api_key": "test-key"}
        ),
    )
    config = PublishedConfig(
        application_id="app",
        agent_version="v0",
        llm_model_id="llm",
        provider="default",
        model=model_name,
        base_url="http://test/v1",
        context_window_tokens=128_000,
        knowledge_base_id="kb",
        temperature=temperature,
        reasoning_effort=effort,
        sampling_config_version=sampling_version,
        loop_guard=PublishedLoopGuard(per_tool_hard_limits={}),
        prompt_version=CURRENT_PROMPT_BUNDLE.prompt_version,
        tool_schema_version=CURRENT_PROMPT_BUNDLE.tool_schema_version,
        checkpoint_schema_version=CURRENT_PROMPT_BUNDLE.checkpoint_schema_version,
        v1_prompt_hash=CURRENT_PROMPT_BUNDLE.v1_prompt_hash,
    )
    with pytest.raises(ModelConstructed):
        service.release_runtime(config, None, dict(knowledge))
    assert requests[-1].get("temperature", "omitted") == (
        expected if sampling_version == 1 else temperature
    )
