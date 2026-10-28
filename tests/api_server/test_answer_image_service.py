from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.api_server.apis.v1.chat.api import router
from app.api_server.common.errors import ApiError, api_error_handler
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.database.models import Document
from app.api_server.repositories.answer_images import AnswerImageRepository
from app.api_server.repositories.sqlite_chat import SQLiteChatRepository
from app.api_server.services.answer_image_service import AnswerImageService
from app.api_server.services.chat_service import ChatService
from app.api_server.storage import validate_image
from app.api_server.storage.filesystem_image_blob_store import FilesystemImageBlobStore
from tests.api_server.test_chat_persistence import _seed_application


@pytest.fixture
def image_context(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "images.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    now = datetime.now(timezone.utc)
    with factory.session_scope(write=True) as session:
        session.add(
            Document(
                id="doc-1",
                knowledge_base_id="kb-1",
                original_filename="source.md",
                file_extension=".md",
                size_bytes=10,
                source_relpath="source.md",
                source_sha256="a" * 64,
                parser="native_markdown",
                status="ready",
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
        )
    buffer = BytesIO()
    with Image.new("RGB", (8, 6), "red") as image:
        image.save(buffer, "PNG")
    raw = buffer.getvalue()
    metadata = validate_image(raw)
    asset = tmp_path / "workspace" / "assets" / "image.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(raw)
    image_id = "img_" + metadata.sha256[:24]
    repository = AnswerImageRepository(factory)
    service = AnswerImageService(
        repository,
        FilesystemImageBlobStore(tmp_path / "archive", profile_id="local"),
        lambda _id: {"workspace_dir": str(tmp_path / "workspace")},
        lambda _id: {"knowledge_base_id": "kb-1"},
        profile_id="local",
    )
    chat = SQLiteChatRepository(factory)
    chat.begin_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        user_message_id="user-1",
        assistant_message_id="assistant-1",
        user_content="show image",
        now=now,
    )
    candidate = {
        "image_id": image_id,
        "asset_relpath": "assets/image.png",
        **metadata.model_dump(),
        "alt_text": "Original diagram",
        "char_offset": 0,
        "char_end": 20,
    }
    result = {
        "answer": f"Answer [1]\n\n![untrusted title](nianlun-image:{image_id})",
        "retrieved_snippets": [
            {
                "doc_id": "doc-1",
                "node_id": "node-1",
                "citation_id": 1,
                "content_version": 1,
                "generation": 1,
                "text": "source",
                "images": [candidate],
            }
        ],
    }
    yield SimpleNamespace(
        factory=factory,
        repository=repository,
        service=service,
        chat=chat,
        raw=raw,
        result=result,
        image_id=image_id,
        now=now,
    )
    factory.dispose()


def save_result(context, result):
    context.chat.complete_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        assistant_message_id="assistant-1",
        answer=result["answer"],
        route="retrieval",
        snippets=result["retrieved_snippets"],
        images=result["images"],
        answer_format_version=result["answer_format_version"],
        answer_output_mode=result["answer_output_mode"],
        image_previews_unavailable=result.get("image_previews_unavailable", 0),
        now=context.now,
    )


def test_archived_image_survives_workspace_removal_and_is_private(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    assert len(result["images"]) == 1
    assert result["answer_format_version"] == 2
    assert result["answer_output_mode"] == "text_and_images"
    attachment = result["images"][0]
    assert attachment["status"] == "available"
    assert attachment["caption"] == "Original diagram"
    assert f"nianlun-image:{attachment['id']}" in result["answer"]
    save_result(context, result)
    (context.service.blob_store.root.parent / "workspace/assets/image.png").unlink()
    data, media_type = context.service.read_content(
        "app-1", "conversation-1", "assistant-1", attachment["id"]
    )
    assert data == context.raw
    assert media_type == "image/png"
    projected = context.service.console_images("app-1", "conversation-1", "assistant-1")
    assert projected[0]["url"].endswith(f"/{attachment['id']}/content")
    assert projected[0]["citation_ids"] == [1]
    assert (
        not {"object_key", "blob_id", "source_bindings", "asset_relpath", "sha256"}
        & projected[0].keys()
    )


@pytest.mark.parametrize("part", [0, 1, 2, 3])
def test_image_read_rejects_every_cross_ownership_dimension(image_context, part):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    identity = ["app-1", "conversation-1", "assistant-1", result["images"][0]["id"]]
    identity[part] = "other"
    with pytest.raises(ApiError) as error:
        context.service.read_content(*identity)
    assert error.value.status_code == 404


def test_deleted_source_revokes_bytes_and_history_projection(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    with context.factory.session_scope(write=True) as session:
        session.get(Document, "doc-1").status = "deleted"
    attachment_id = result["images"][0]["id"]
    with pytest.raises(ApiError) as error:
        context.service.read_content(
            "app-1", "conversation-1", "assistant-1", attachment_id
        )
    assert error.value.status_code == 404
    projected = context.service.console_images("app-1", "conversation-1", "assistant-1")
    assert projected[0]["status"] == "unavailable"
    assert projected[0]["url"] is None
    assert projected[0]["unavailable_reason"] == "source_deleted"


def test_source_deleted_before_completion_cannot_create_available_attachment(
    image_context,
):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    with context.factory.session_scope(write=True) as session:
        session.get(Document, "doc-1").status = "deleted"
    save_result(context, result)
    persisted = context.repository.message_images("assistant-1")
    assert persisted[0]["status"] == "unavailable"
    assert persisted[0]["unavailable_reason"] == "source_deleted"


def test_source_preview_does_not_become_answer_image(image_context):
    context = image_context
    result = context.service.prepare_result(
        "app-1", {**context.result, "answer": "Text only"}
    )
    assert result["answer_output_mode"] == "text"
    assert len(result["images"]) == 1
    assert result["images"][0]["display_in_answer"] is False
    save_result(context, result)
    assert context.service.console_images("app-1", "conversation-1", "assistant-1")[0][
        "url"
    ]


@pytest.mark.parametrize(
    ("answer", "before", "after"),
    [
        ("Intro [1]\n\nNext paragraph", "Intro [1]", "Next paragraph"),
        ("- First [1]\n- Second\n\nNext paragraph", "- Second", "Next paragraph"),
        ("| Value |\n| --- |\n| Data [1] |\n\nNext", "| Data [1] |", "Next"),
        ("> Quoted [1]\n\nNext", "> Quoted [1]", "Next"),
        ("Intro [1]\r\n\r\nNext", "Intro [1]", "Next"),
    ],
)
def test_cited_source_images_are_inserted_after_complete_markdown_blocks(
    image_context, answer, before, after
):
    result = image_context.service.prepare_result(
        "app-1", {**image_context.result, "answer": answer}
    )
    assert result["answer_output_mode"] == "text_and_images"
    attachment = result["images"][0]
    assert attachment["display_in_answer"] is True
    assert attachment["status"] == "available"
    assert len(attachment["occurrences"]) == 1
    marker = f"![Original diagram](nianlun-image:{attachment['id']})"
    assert result["answer"].index(before) < result["answer"].index(marker)
    assert result["answer"].index(marker) < result["answer"].index(after)
    occurrence = attachment["occurrences"][0]
    assert result["answer"][occurrence["start"] : occurrence["end"]] == marker
    save_result(image_context, result)
    assert (
        image_context.chat.get_messages("app-1", "conversation-1")[-1]["content"]
        == result["answer"]
    )


@pytest.mark.parametrize(
    "answer",
    [
        "No citation",
        "Other [2]",
        "`Example [1]`",
        "```md\nExample [1]\n```",
        "[1](https://example.test)",
        "$value [1]$",
    ],
)
def test_uncited_or_literal_citations_do_not_automatically_add_images(
    image_context, answer
):
    result = image_context.service.prepare_result(
        "app-1", {**image_context.result, "answer": answer}
    )
    assert result["answer"] == answer
    assert result["answer_output_mode"] == "text"
    assert result["images"][0]["display_in_answer"] is False


def test_automatic_images_respect_answer_input_scope(image_context):
    result = image_context.service.prepare_result(
        "app-1",
        {**image_context.result, "answer": "Text [1]", "answer_input_citation_ids": []},
    )
    assert result["answer"] == "Text [1]"
    assert result["images"][0]["display_in_answer"] is False


@pytest.mark.parametrize("excluded_explicit", [False, True])
def test_auto_image_alias_must_use_the_eligible_answer_input(
    image_context, excluded_explicit
):
    from copy import deepcopy

    source = deepcopy(image_context.result)
    second = deepcopy(source["retrieved_snippets"][0])
    second["citation_id"] = 2
    second["images"][0]["ref_id"] = "img_02"
    source["retrieved_snippets"].append(second)
    source.update(answer="Text [2]", answer_input_citation_ids=[2])
    if excluded_explicit:
        source["answer"] += f"\n\n![Excluded](nianlun-image:{image_context.image_id})"
    result = image_context.service.prepare_result("app-1", source)
    assert result["answer_output_mode"] == "text_and_images"
    assert result["images"][0]["occurrences"]


def test_auto_image_reserves_space_for_the_archived_attachment_id(image_context):
    from dataclasses import replace
    from nianlun.agent.contracts import AnswerImagePolicy

    result = image_context.service.prepare_result(
        "app-1",
        {**image_context.result, "answer": "Text [1]"},
        policy=replace(AnswerImagePolicy(), max_candidate_line_bytes=68),
    )
    assert result["answer"] == "Text [1]"
    assert result["images"][0]["display_in_answer"] is False


def test_repeated_citations_add_the_same_resource_only_once(image_context):
    from copy import deepcopy

    source = deepcopy(image_context.result)
    second = deepcopy(source["retrieved_snippets"][0])
    second["citation_id"] = 2
    second["images"][0]["ref_id"] = "img_02"
    source["retrieved_snippets"].append(second)
    source["answer"] = "First [1]\n\nSecond [2]\n\nThird [1]"
    result = image_context.service.prepare_result("app-1", source)
    assert result["answer_output_mode"] == "text_and_images"
    assert len(result["images"]) == 1
    assert len(result["images"][0]["occurrences"]) == 1
    assert len(result["images"][0]["source_bindings"]) == 2


@pytest.mark.parametrize("offset", [0, 500])
def test_source_image_coordinates_control_answer_placement(image_context, offset):
    from copy import deepcopy

    source = deepcopy(image_context.result)
    text = (
        "全程约 530 公里，车程约 8 小时。\n\n"
        "游览大柴旦翡翠湖，之后入住大柴旦酒店休息。\n\n"
        "![](assets/image.png)\n\n"
        "高原反应通常两天后缓解，请自备药物并保证睡眠。"
    )
    snippet = source["retrieved_snippets"][0]
    snippet.update(text=text, char_offset=offset)
    first = text.index("![]")
    last = first + len("![](assets/image.png)")
    snippet["images"][0].update(source_start=offset + first, source_end=offset + last)
    source["answer"] = (
        "行程概况：全程约 530 公里，车程约 8 小时。[1]\n\n"
        "游览大柴旦翡翠湖，之后入住大柴旦酒店休息。[1]\n\n"
        "高原反应通常两天后缓解，请自备药物并保证睡眠。[1]"
    )
    result = image_context.service.prepare_result("app-1", source)
    answer = result["answer"]
    marker = "nianlun-image:" + result["images"][0]["id"]
    assert answer.index("翡翠湖") < answer.index(marker) < answer.index("高原反应")
    assert source["retrieved_snippets"][0]["text"] == text


def test_consecutive_source_images_keep_context_and_order(image_context, tmp_path):
    from copy import deepcopy

    source = deepcopy(image_context.result)
    text = (
        "游览大柴旦翡翠湖，之后入住酒店休息。\n\n"
        "![](first.png)\n\n![](second.png)\n\n高原反应请自备药物。"
    )
    first = source["retrieved_snippets"][0]["images"][0]
    first.update(
        source_start=text.index("![](first"),
        source_end=text.index("![](first") + len("![](first.png)"),
    )
    buffer = BytesIO()
    with Image.new("RGB", (8, 6), "blue") as image:
        image.save(buffer, "PNG")
    raw = buffer.getvalue()
    second = {
        **first,
        **validate_image(raw).model_dump(),
        "image_id": "img_second",
        "ref_id": "img_02",
        "asset_relpath": "assets/second.png",
        "source_start": text.index("![](second"),
        "source_end": text.index("![](second") + len("![](second.png)"),
    }
    (tmp_path / "workspace/assets/second.png").write_bytes(raw)
    source["retrieved_snippets"][0].update(text=text, images=[second, first])
    source["answer"] = (
        "游览大柴旦翡翠湖，之后入住酒店休息。[1]\n\n高原反应请自备药物。[1]"
    )
    result = image_context.service.prepare_result("app-1", source)
    images = result["images"]
    assert [item["image_id"] for item in images] == [
        first["image_id"],
        second["image_id"],
    ]
    assert result["answer"].index("nianlun-image:" + images[1]["id"]) < result[
        "answer"
    ].index("高原反应")


def test_automatic_image_failure_preserves_the_text_answer(image_context):
    from copy import deepcopy

    source = deepcopy(image_context.result)
    source["answer"] = "Text [1]"
    source["retrieved_snippets"][0]["images"][0]["asset_relpath"] = "missing.png"
    result = image_context.service.prepare_result("app-1", source)
    assert result["answer_output_mode"] == "text"
    assert result["answer"].startswith("Text [1]")
    assert result["images"][0]["status"] == "unavailable"


def test_request_ref_resolves_to_archived_full_source_identity(image_context):
    from copy import deepcopy

    context = image_context
    source = deepcopy(context.result)
    candidate = source["retrieved_snippets"][0]["images"][0]
    candidate.update(ref_id="img_01", content_version=7, generation=2)
    source["answer"] = "Answer [1]\n\n![model](nianlun-image:img_01)"
    source["answer_input_citation_ids"] = [1]
    result = context.service.prepare_result("app-1", source)
    image = result["images"][0]
    assert image["status"] == "available"
    assert image["image_id"] == context.image_id
    assert image["ref_ids"] == ["img_01"]
    assert image["source_bindings"][0]["content_version"] == 7
    assert f"nianlun-image:{image['id']}" in result["answer"]


def test_pure_image_result_archives_selection_without_text_citations(image_context):
    from copy import deepcopy

    context = image_context
    source = deepcopy(context.result)
    candidate = source["retrieved_snippets"][0]["images"][0]
    candidate["ref_id"] = "img_01"
    source.update(
        answer="",
        answer_output_mode="images",
        selected_answer_images=[candidate],
        answer_input_citation_ids=[],
    )
    result = context.service.prepare_result("app-1", source)
    assert result["answer"] == ""
    assert result["answer_output_mode"] == "images"
    assert len(result["images"]) == 1
    assert result["images"][0]["status"] == "available"
    assert result["images"][0]["display_in_answer"] is True
    assert result["display_image_citation_ids"] == [1]


def test_unavailable_pure_selection_returns_visible_failure(image_context):
    from copy import deepcopy

    source = deepcopy(image_context.result)
    candidate = source["retrieved_snippets"][0]["images"][0]
    candidate.update(ref_id="img_01", asset_relpath="missing.png")
    source.update(
        answer="",
        answer_output_mode="images",
        selected_answer_images=[candidate],
        answer_input_citation_ids=[],
    )
    result = image_context.service.prepare_result("app-1", source)
    assert result["answer_output_mode"] == "text"
    assert result["answer"].strip()


def test_same_resource_refs_share_attachment_and_keep_all_sources(image_context):
    from copy import deepcopy

    context = image_context
    source = deepcopy(context.result)
    first = source["retrieved_snippets"][0]
    first["images"][0]["ref_id"] = "img_01"
    second = deepcopy(first)
    second.update(citation_id=2, node_id="node-2")
    second["images"][0]["ref_id"] = "img_02"
    source["retrieved_snippets"].append(second)
    source["answer"] = (
        "Text [1][2]\n\n![one](nianlun-image:img_01)\n\n![two](nianlun-image:img_02)"
    )
    result = context.service.prepare_result("app-1", source)
    assert len(result["images"]) == 1
    image = result["images"][0]
    assert image["ref_ids"] == ["img_01", "img_02"]
    assert len(image["source_bindings"]) == 2
    assert len(image["occurrences"]) == 2


@pytest.mark.parametrize("agent_version", ["v0", "v1"])
@pytest.mark.parametrize("stream", [False, True])
def test_real_v5_runtime_selects_and_archives_original_image(
    image_context, monkeypatch, agent_version, stream
):
    import json
    from copy import deepcopy
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from nianlun.agent.lead_agent.factory import AgentRuntimeFactory
    from nianlun.agent.prompt_registry import CURRENT_PROMPT_BUNDLE

    context = image_context
    candidate = deepcopy(context.result["retrieved_snippets"][0]["images"][0])
    source_text = "![Original diagram](assets/image.png)"
    candidate.update(
        knowledge_base_id="kb-1",
        content_version=1,
        generation=1,
        source_start=0,
        source_end=len(source_text),
    )

    class KnowledgeBase:
        has_fts = True
        has_vector = False

        def get_line_content(self, **kwargs):
            return json.dumps(
                {
                    "doc_id": "doc-1",
                    "doc_name": "Source",
                    "line_spec": "1",
                    "content": [
                        {
                            "node_id": "node-1",
                            "line_num": 1,
                            "text": source_text,
                            "images": [candidate],
                            "image_spans": [
                                {
                                    "source_start": 0,
                                    "source_end": len(source_text),
                                    "kind": "markdown",
                                }
                            ],
                        }
                    ],
                }
            )

    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            message = self._generate(messages, stop=stop).generations[0].message
            # GenericFakeChatModel does not stream the modern tool_calls field.
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content=message.content,
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "id": call["id"],
                            "args": json.dumps(call["args"]),
                            "index": index,
                        }
                        for index, call in enumerate(message.tool_calls)
                    ],
                    chunk_position="last",
                )
            )
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk

    model = Model(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_line_content",
                            "args": {"doc_id": "doc-1", "line_spec": "1"},
                            "id": "read",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "select_answer_images",
                            "args": {"mode": "images", "image_refs": ["img_01"]},
                            "id": "select",
                        }
                    ],
                ),
                AIMessage(content="candidate must be suppressed"),
            ]
        )
    )
    monkeypatch.setattr(
        "nianlun.agent.lead_agent.factory.build_chat_model", lambda **_: model
    )
    runtime = AgentRuntimeFactory(
        knowledge_base=KnowledgeBase(),
        model="fake",
        api_key="fake",
        allow_env_fallback=False,
        agent_version=agent_version,
        prompt_bundle=CURRENT_PROMPT_BUNDLE,
        image_capability=True,
    ).create()
    if stream:
        events = list(runtime.iter_events("Show original diagram"))
        assert not [event for event in events if event["type"] == "message"]
        raw_result = events[-1]["data"]
    else:
        raw_result = runtime.invoke("Show original diagram")
    assert raw_result["answer_output_mode"] == "images"
    assert raw_result["answer"] == ""
    assert [call["name"] for call in raw_result["tool_calls"]] == [
        "get_line_content",
        "select_answer_images",
    ]
    archived = context.service.prepare_result("app-1", raw_result)
    assert archived["answer_output_mode"] == "images"
    assert archived["images"][0]["status"] == "available"
    save_result(context, archived)
    data, _ = context.service.read_content(
        "app-1", "conversation-1", "assistant-1", archived["images"][0]["id"]
    )
    assert data == context.raw


def test_content_route_sets_media_security_headers(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.state.services = SimpleNamespace(images=context.service)
    app.include_router(router)
    url = (
        "/api/v1/apps/app-1/conversations/conversation-1/messages/assistant-1/images/"
        + result["images"][0]["id"]
        + "/content"
    )
    with TestClient(app) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == context.raw
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get(url.replace("app-1", "other")).status_code == 404


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("automatic", [False, True])
def test_console_completion_and_history_share_whitelisted_images(
    image_context, monkeypatch, stream, automatic
):
    context = image_context
    if automatic:
        context.result["answer"] = "Answer [1]"
    import app.api_server.services.chat_service as module

    monkeypatch.setattr(module, "run_agent", lambda *args, **kwargs: context.result)
    monkeypatch.setattr(
        module,
        "iter_agent_stream_events",
        lambda *args, **kwargs: iter([{"type": "done", "data": context.result}]),
    )
    service = ChatService(
        SimpleNamespace(
            require_record=lambda _id: {}, runtime=lambda _id, **kwargs: object()
        ),
        context.chat,
        context.service,
    )
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.state.services = SimpleNamespace(chat=service, images=context.service)
    app.include_router(router)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/apps/app-1/chat",
                json={
                    "message": "show image",
                    "conversation_id": "conversation-2",
                    "response_mode": "streaming" if stream else "blocking",
                },
            )
            assert response.status_code == 200
            if stream:
                import json

                event = response.text.split("event: done\n", 1)[1].split("\n\n", 1)[0]
                result = json.loads(event.removeprefix("data: "))
            else:
                result = response.json()["data"]
            assert result["answer_format_version"] == 2
            assert result["answer_output_mode"] == "text_and_images"
            assert len(result["images"]) == 1
            assert result["images"][0]["url"]
            assert "asset_relpath" not in str(result)
            assert "source_bindings" not in str(result)
            history = client.get(
                "/api/v1/apps/app-1/conversations/conversation-2/messages"
            ).json()["data"]
            assert history[-1]["images"] == result["images"]
            assert history[-1]["answer_format_version"] == 2
            source_id = history[-1]["sources"][0]["id"]
            source = client.get(
                f"/api/v1/apps/app-1/conversations/conversation-2/messages/{result['message_id']}/sources/{source_id}"
            ).json()["data"]
            assert source["image_attachment_ids"] == [result["images"][0]["id"]]
            assert "asset_relpath" not in str(source)
    finally:
        service.shutdown()
