"""Exercise response parsing with the installed OpenAI SDK, without a model."""

import base64
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import OpenAI

from app.api_server.apis.v1.router import v1_router
from app.api_server.common.errors import ApiError, api_error_handler
from tests.api_server.test_application_publishing import publishing as publishing
from tests.api_server.test_public_runtime import runtime as runtime
from tests.api_server.test_public_answer_images import image_runtime as image_runtime


@pytest.mark.parametrize("transport", ["url", "data_url"])
@pytest.mark.parametrize("streaming", [False, True])
def test_openai_sdk_parses_image_result_and_idempotent_replay(
    image_runtime, transport, streaming
):
    context = image_runtime
    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=context.service)
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(v1_router)
    with (
        TestClient(app) as http,
        OpenAI(
            base_url="http://testserver/v1",
            api_key=context.token,
            http_client=http,
            max_retries=0,
        ) as sdk,
    ):
        answers, locations = [], []
        for _ in range(2):
            raw = sdk.chat.completions.with_raw_response.create(
                model=context.app_id,
                messages=[{"role": "user", "content": "show image"}],
                user="user",
                stream=streaming,
                extra_headers={
                    "Idempotency-Key": "sdk-image-key",
                    "X-Nianlun-Include-Images": "true",
                    "X-Nianlun-Image-Transport": transport,
                },
            )
            locations.append(raw.headers["location"])
            assert raw.headers["cache-control"] == "private, no-store"
            result = raw.parse()
            if streaming:
                with result:
                    chunks = list(result)
                answers.append(
                    "".join(
                        choice.delta.content or ""
                        for chunk in chunks
                        for choice in chunk.choices
                    )
                )
                assert chunks[-1].choices[0].finish_reason == "stop"
            else:
                answers.append(result.choices[0].message.content)
                assert result.choices[0].finish_reason == "stop"
        assert answers[0] == answers[1]
        assert locations[0] == locations[1]
        path = urlsplit(locations[0]).path
        assert path.startswith(f"/api/v1/runtime/apps/{context.app_id}/runs/")
        detail = http.get(
            path,
            params={"user_id": "user"},
            headers={"Authorization": f"Bearer {context.token}"},
        )
        assert detail.status_code == 200
        result = detail.json()["data"]["result"]
        assert answers[0] == result["answer"]
        assert "nianlun-image:" not in answers[0]
        if transport == "data_url":
            assert (
                base64.b64decode(result["images"][0]["url"].split(",", 1)[1])
                == context.raw_image
            )
            assert result["images"][0]["expires_at"] is None
        else:
            assert result["images"][0]["url"].startswith(
                "https://api.test/api/v1/runtime/images/"
            )
            assert result["images"][0]["expires_at"]
    assert len(context.calls) == 1
