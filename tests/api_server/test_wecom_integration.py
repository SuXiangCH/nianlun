"""Cross-layer contract: management API, gateway internal API and conversations."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_server.apis.internal.router import internal_router
from app.api_server.apis.v1.apps.publishing import router as publishing_router
from app.api_server.apis.v1.chat.api import router as chat_router
from app.api_server.apis.v1.schemas import ApplicationCreateRequest
from app.api_server.common.errors import ApiError, api_error_handler
from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.repositories.publishing import PublishingRepository
from app.api_server.repositories.sqlite_chat import SQLiteChatRepository
from app.api_server.repositories.sqlite_metadata import SQLiteMetadataRepository
from app.api_server.repositories.wecom_channels import WecomChannelRepository
from app.api_server.services.application_invocation_service import (
    ApplicationInvocationService,
)
from app.api_server.services.application_release_service import (
    ApplicationReleaseService,
)
from app.api_server.services.application_service import ApplicationService
from app.api_server.services.application_wecom_service import ApplicationWecomService
from app.api_server.services.chat_service import ChatService
from app.api_server.services.model_config_service import ModelConfigService

TOKEN = "i" * 32
CONTROL_TOKEN = "c" * 32
USER_KEY = "wc_" + "a" * 32
SECRET = "integration-secret"


class FakeGatewayControl:
    def __init__(self) -> None:
        self.refreshes = 0
        self.connects: list[tuple[str, int]] = []

    def refresh(self) -> None:
        self.refreshes += 1

    def connect(self, channel_id: str, revision: int) -> dict[str, object]:
        self.connects.append((channel_id, revision))
        return {"channel_id": channel_id, "status": "online"}


@pytest.fixture
def integration(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, MessagesState, StateGraph

    factory = SQLiteConnectionFactory(tmp_path / "wecom-integration.sqlite3")
    initialize_database(factory)
    metadata = SQLiteMetadataRepository(factory)
    now = datetime.now(timezone.utc).isoformat()
    metadata.create_knowledge_base_with_revision(
        {
            "id": "kb",
            "name": "Knowledge",
            "created_at": now,
            "updated_at": now,
        },
        snapshot_relpath="snapshots/r1",
        manifest_sha256="a" * 64,
    )
    metadata.create_model_profile(
        {
            "id": "llm",
            "kind": "llm",
            "name": "Model",
            "model": "test-model",
            "base_url": "https://models.test/v1",
            "api_key": "model-credential",
            "context_window_tokens": 64_000,
        },
        now,
    )
    applications = ApplicationService(
        metadata,
        lambda _: {
            "id": "kb",
            "status": "ready",
            "snapshot_relpath": "snapshots/r1",
            "snapshot_manifest_sha256": "a" * 64,
            "content_version": 1,
        },
        model_config_service=ModelConfigService(metadata),
    )
    application = applications.create(
        ApplicationCreateRequest(name="App", knowledge_base_id="kb")
    )
    releases = ApplicationReleaseService(PublishingRepository(factory), applications)
    control = FakeGatewayControl()
    settings = ApiServerSettings(
        data_dir=tmp_path,
        internal_token=TOKEN,
        gateway_control_urls=("http://gateway.test",),
        gateway_control_token=CONTROL_TOKEN,
    )
    wecom = ApplicationWecomService(
        WecomChannelRepository(factory), releases, applications, settings, control
    )
    invocations = ApplicationInvocationService(
        releases, applications, factory, settings
    )
    chat = ChatService(applications, SQLiteChatRepository(factory))

    def build_runtime(config, checkpointer, record):
        builder = StateGraph(MessagesState)
        builder.add_node(
            "answer",
            lambda state: {
                "messages": [AIMessage(content=f"answer {len(state['messages'])}")]
            },
        )
        builder.add_edge(START, "answer")
        builder.add_edge("answer", END)
        graph = builder.compile(checkpointer=checkpointer)

        def events(message, thread_id, **kwargs):
            yield {"type": "message", "data": {"delta": "candidate"}}
            result = graph.invoke(
                {"messages": [HumanMessage(content=message)]},
                {"configurable": {"thread_id": thread_id}},
            )
            yield {
                "type": "done",
                "data": {
                    "answer": result["messages"][-1].content,
                    "retrieved_snippets": [
                        {
                            "doc_id": "doc",
                            "node_id": "node",
                            "line_spec": "1",
                            "text": "evidence",
                            "path": "/private/path",
                        }
                    ],
                },
            }

        return SimpleNamespace(iter_events=events)

    monkeypatch.setattr(applications, "release_runtime", build_runtime)
    monkeypatch.setattr(applications, "release_runtime_resources", lambda runtime: None)

    app = FastAPI()
    app.state.settings = settings
    app.state.services = SimpleNamespace(
        publishing=releases,
        wecom=wecom,
        invocations=invocations,
        chat=chat,
    )
    app.include_router(publishing_router)
    app.include_router(chat_router)
    app.include_router(internal_router)
    app.add_exception_handler(ApiError, api_error_handler)
    with TestClient(app) as http:
        yield SimpleNamespace(
            http=http,
            app_id=application.id,
            path=f"/api/v1/apps/{application.id}",
            control=control,
            releases=releases,
            chat=chat,
        )
    invocations.close()
    factory.dispose()


def _internal_headers(idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer " + TOKEN}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _snapshot(integration) -> list[dict[str, object]]:
    response = integration.http.get(
        "/internal/v1/wecom/snapshot", headers=_internal_headers()
    )
    assert response.status_code == 200
    return response.json()["channels"]


def test_wecom_channel_contract_from_creation_to_disable(integration):
    http = integration.http
    path = integration.path

    created = http.post(
        f"{path}/channels",
        json={
            "type": "wecom",
            "bot_id": "bot-integration",
            "secret": SECRET,
            "default_response": "稍后再试",
        },
    )
    assert created.status_code == 200
    assert SECRET not in created.text
    state = created.json()["data"]["wecom_channel"]
    channel_id = state["id"]
    assert state["enabled"] is False
    assert state["has_secret"] is True
    assert state["config_revision"] == 1
    assert integration.control.refreshes == 1
    assert _snapshot(integration) == []

    release = http.post(f"{path}/releases", json={"expected_config_version": 1}).json()[
        "data"
    ]
    deployed = http.post(
        f"{path}/deployments",
        json={
            "channel_type": "wecom",
            "release_id": release["id"],
            "expected_deployment_version": 0,
        },
    )
    assert deployed.status_code == 200
    state = deployed.json()["data"]["wecom_channel"]
    revision = state["config_revision"]
    assert state["enabled"] is True
    assert state["active_release"]["id"] == release["id"]
    assert state["deployment_version"] == 1

    [channel] = _snapshot(integration)
    assert channel == {
        "channel_id": channel_id,
        "application_id": integration.app_id,
        "revision": revision,
        "enabled": True,
        "connection": {"bot_id": "bot-integration", "secret": SECRET},
        "runtime": {"default_response": "稍后再试", "expires_at": None},
    }

    status = http.post(
        f"/internal/v1/wecom/channels/{channel_id}/status",
        json={"revision": revision, "status": "online"},
        headers=_internal_headers(),
    )
    assert status.status_code == 204
    state = http.get(f"{path}/channels/wecom").json()["data"]["wecom_channel"]
    assert state["status"] == "online"
    assert state["error_code"] is None
    assert state["last_reported_at"] is not None

    run = http.post(
        f"/internal/v1/wecom/channels/{channel_id}/runs",
        json={
            "revision": revision,
            "message": {"user_key": USER_KEY, "text": "你好"},
        },
        headers=_internal_headers("msg-1"),
    )
    assert run.status_code == 200
    assert run.headers["Content-Type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in run.text.splitlines() if line]
    assert [event["type"] for event in events] == ["start", "chunk", "done"]
    assert events[1]["content"] == "candidate"
    assert events[2]["content"] == "answer 1"
    assert events[0]["release_id"] == release["id"]
    assert SECRET not in run.text

    replay = http.post(
        f"/internal/v1/wecom/channels/{channel_id}/runs",
        json={
            "revision": revision,
            "message": {"user_key": USER_KEY, "text": "你好"},
        },
        headers=_internal_headers("msg-1"),
    )
    assert replay.status_code == 204

    conversations = http.get(
        f"{path}/conversations", params={"channel": "wecom"}
    ).json()["data"]
    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation["id"] == events[0]["conversation_id"]
    assert conversation["channel"] == "wecom"
    assert conversation["external_user_id"] == USER_KEY
    assert "你好" in conversation["title"]
    messages = http.get(f"{path}/conversations/{conversation['id']}/messages").json()[
        "data"
    ]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert [message["content"] for message in messages] == ["你好", "answer 1"]

    disabled = http.patch(
        f"{path}/channels/wecom",
        json={
            "enabled": False,
            "expected_deployment_version": state["deployment_version"],
        },
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["wecom_channel"]["enabled"] is False
    assert _snapshot(integration) == []
    assert integration.control.refreshes == 3

    refused = http.post(
        f"/internal/v1/wecom/channels/{channel_id}/runs",
        json={
            "revision": disabled.json()["data"]["wecom_channel"]["config_revision"],
            "message": {"user_key": USER_KEY, "text": "again"},
        },
        headers=_internal_headers("msg-2"),
    )
    assert refused.status_code == 403
