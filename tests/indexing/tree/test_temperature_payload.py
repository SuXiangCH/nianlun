"""Document helper calls preserve the configured provider temperature policy."""

from __future__ import annotations

import json
import asyncio

import httpx
import pytest

import nianlun.models.llm as llm_module
from nianlun.indexing.tree.heading_recovery import recover_heading_levels_with_llm
from nianlun.indexing.tree.llm import describe_document, summarize_node
from nianlun.indexing.tree.untitled.models import PlannerConfig
from nianlun.indexing.tree.untitled.planner import _call


@pytest.mark.parametrize("helper", ["summary", "description", "headings", "untitled"])
@pytest.mark.parametrize("effort", [None, "high"])
@pytest.mark.parametrize("temperature", [None, 0.0, 0.65])
def test_document_helpers_preserve_configured_temperature_on_wire(
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
    effort: str | None,
    temperature: float | None,
) -> None:
    requests: list[dict[str, object]] = []
    content = '{"levels": {"0": 1}, "ok": true}'

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "helper-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async def exercise() -> None:
        chat_class = llm_module.ChatOpenAI
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(
                llm_module,
                "ChatOpenAI",
                lambda **kwargs: chat_class(**kwargs, http_async_client=client),
            )
            model = llm_module.build_chat_model(
                model="test-model",
                api_key="test-key",
                base_url="http://test/v1",
                allow_env_fallback=False,
                temperature=temperature,
                reasoning_effort=effort,  # type: ignore[arg-type]
            )
            if helper == "summary":
                assert await summarize_node(model, "Document text") == content
            elif helper == "description":
                assert await describe_document(model, [{"title": "Report"}]) == content
            elif helper == "headings":
                result = await recover_heading_levels_with_llm(
                    [{"title": "Report", "line_num": 1, "level": 1}], model
                )
                assert result.mode == "rules_then_llm"
            else:
                assert (
                    await _call(model, "system", "source", PlannerConfig(max_retries=0))
                )["ok"] is True

    asyncio.run(exercise())

    assert len(requests) == 1
    assert requests[0].get("reasoning_effort") == effort
    if temperature is None:
        assert "temperature" not in requests[0]
    else:
        assert requests[0]["temperature"] == temperature
