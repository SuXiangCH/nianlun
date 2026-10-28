import base64
import hashlib
import json

import pytest

from app.api_server.apis.v1.runtime_schemas import PublicChatRequest, PublicChatResponse
from app.api_server.common.errors import ApiError
from app.api_server.services.answer_image_projection import (
    AnswerImageProjection,
    InlineProjection,
)


def response(answer):
    return PublicChatResponse(
        app_id="app",
        release_id="release",
        run_id="run",
        conversation_id="conv",
        message_id="message",
        answer=answer,
        knowledge_base_content_version=1,
    )


def attachment(id="attachment-1", *, preview=False, size=3):
    return {
        "id": id,
        "image_id": "img_one",
        "ref_ids": ["img_one"],
        "caption": "Original [diagram]",
        "source_bindings": [{"citation_id": 2}],
        "display_in_answer": not preview,
        "status": "available",
        "unavailable_reason": None,
        "media_type": "image/png",
        "size_bytes": size,
        "width": 8,
        "height": 6,
        "orientation": 1,
        "blob_id": "blob-1",
        "sha256": hashlib.sha256(b"abc").hexdigest(),
    }


def test_transport_request_is_strict_and_defaults_remain_text():
    body = PublicChatRequest(message="hello", user_id="u")
    assert body.include_images is False
    assert body.image_transport == "url"
    for options in [
        {"image_transport": "data_url"},
        {"include_images": "true"},
        {"include_images": True, "image_transport": "ftp"},
    ]:
        with pytest.raises(ValueError):
            PublicChatRequest(message="hello", user_id="u", **options)


def test_url_projection_preserves_positions_and_filters_source_previews():
    projector = AnswerImageProjection()
    initial = response("Before\n\n![untrusted](nianlun-image:attachment-1)\n\nAfter")
    result, stored = projector.prepare(
        initial,
        [attachment(), attachment("preview", preview=True)],
        transport="url",
        read_bytes=lambda item: b"abc",
        url_for=lambda item: ("https://api.test/image?token=opaque", "expiry"),
    )
    assert (
        result.answer
        == "Before\n\n![Original \\[diagram\\]](https://api.test/image?token=opaque)\n\nAfter"
    )
    assert [item.id for item in result.images] == ["attachment-1"]
    assert result.images[0].url == "https://api.test/image?token=opaque"
    assert "nianlun-image:" not in result.answer
    assert "blob_id" not in result.model_dump_json()
    assert stored == result.model_dump(mode="json")


def test_inline_projection_replays_without_persisting_base64_and_deduplicates_answer():
    projector = AnswerImageProjection()
    initial = response(
        "![one](nianlun-image:attachment-1)\n\n![repeat](nianlun-image:attachment-1)"
    )
    data_url = "data:image/png;base64," + base64.b64encode(b"abc").decode()
    result, stored = projector.prepare(
        initial,
        [attachment()],
        transport="data_url",
        read_bytes=lambda item: b"abc",
    )
    assert result.answer.count(data_url) == 1
    assert "见前图" in result.answer
    assert result.images[0].url == data_url
    assert result.images[0].expires_at is None
    assert "base64" not in json.dumps(stored)
    assert stored["storage_format"] == "inline_projection_v1"
    plan = InlineProjection.model_validate(stored)
    assert projector.replay_inline(plan, read_bytes=lambda item: b"abc") == result
    revoked = projector.replay_inline(plan, read_bytes=lambda item: None)
    assert revoked.images[0].url is None
    assert revoked.images[0].status == "unavailable"
    assert "data:" not in revoked.answer
    assert revoked.answer_output_mode == "text"


def test_inline_budget_rejects_large_image_and_allows_later_smaller_one():
    projector = AnswerImageProjection(inline_max_bytes=4, inline_total_bytes=4)
    result, stored = projector.prepare(
        response("![large](nianlun-image:large)\n\n![small](nianlun-image:small)"),
        [attachment("large", size=5), attachment("small", size=3)],
        transport="data_url",
        read_bytes=lambda item: b"abc",
    )
    assert result.images[0].unavailable_reason == "inline_budget_exceeded"
    assert result.images[0].url is None
    assert result.images[1].url.startswith("data:image/png;base64,")
    assert "base64" not in json.dumps(stored)


def test_inline_full_envelope_budget_is_enforced():
    projector = AnswerImageProjection(inline_response_max_bytes=10)
    with pytest.raises(ApiError, match="响应") as error:
        projector.prepare(
            response("Text"), [], transport="data_url", read_bytes=lambda item: b""
        )
    assert error.value.status_code == 413


def test_inline_replay_keeps_frozen_budget_failures():
    projector = AnswerImageProjection(inline_max_bytes=2)
    _, stored = projector.prepare(
        response("![one](nianlun-image:attachment-1)"),
        [attachment()],
        transport="data_url",
        read_bytes=lambda item: b"abc",
    )
    replay = AnswerImageProjection().replay_inline(
        InlineProjection.model_validate(stored), read_bytes=lambda item: b"abc"
    )
    assert replay.images[0].unavailable_reason == "inline_budget_exceeded"
    assert replay.images[0].url is None


def test_inline_replay_rejects_same_size_bytes_with_different_digest():
    projector = AnswerImageProjection()
    _, stored = projector.prepare(
        response("![one](nianlun-image:attachment-1)"),
        [attachment()],
        transport="data_url",
        read_bytes=lambda item: b"abc",
    )
    with pytest.raises(ValueError, match="摘要"):
        projector.replay_inline(
            InlineProjection.model_validate(stored), read_bytes=lambda item: b"xyz"
        )


@pytest.mark.parametrize("transport", ["url", "data_url"])
def test_completion_source_loss_reconciles_frozen_public_projection(transport):
    from app.api_server.services.answer_image_projection import (
        reconcile_completion_projection,
    )

    projector = AnswerImageProjection()
    _, stored = projector.prepare(
        response("Before\n\n![one](nianlun-image:attachment-1)\n\nAfter"),
        [attachment()],
        transport=transport,
        read_bytes=lambda item: b"abc",
        url_for=lambda item: ("https://api.test/image?token=opaque", "expiry"),
    )
    raw = {
        "answer": "Before\n\n图片暂不可用\n\nAfter",
        "answer_output_mode": "text",
        "images": [
            {
                **attachment(),
                "status": "unavailable",
                "unavailable_reason": "source_deleted",
                "source_bindings": [],
            }
        ],
    }
    reconciled = reconcile_completion_projection(stored, raw)
    if transport == "data_url":
        plan = InlineProjection.model_validate(reconciled)
        assert plan.bindings == []
        result = projector.replay_inline(
            plan, read_bytes=lambda item: pytest.fail("revoked IO")
        )
    else:
        result = PublicChatResponse.model_validate(reconciled)
    assert result.answer == "Before\n\n图片暂不可用\n\nAfter"
    assert result.answer_output_mode == "text"
    assert result.images[0].status == "unavailable"
    assert result.images[0].unavailable_reason == "source_deleted"
    assert result.images[0].citation_ids == []
    assert result.images[0].url is None
    assert "token=" not in json.dumps(reconciled)
    assert "base64" not in json.dumps(reconciled)


def test_completion_keeps_frozen_url_and_only_remaining_citations():
    from app.api_server.services.answer_image_projection import (
        reconcile_completion_projection,
    )

    _, stored = AnswerImageProjection().prepare(
        response("![one](nianlun-image:attachment-1)"),
        [attachment()],
        transport="url",
        read_bytes=lambda item: b"abc",
        url_for=lambda item: ("https://api.test/image?token=opaque", "expiry"),
    )
    raw = {
        "answer": "![one](nianlun-image:attachment-1)",
        "answer_output_mode": "images",
        "images": [{**attachment(), "source_bindings": [{"citation_id": 3}]}],
    }
    result = PublicChatResponse.model_validate(
        reconcile_completion_projection(stored, raw)
    )
    assert result.images[0].citation_ids == [3]
    assert result.images[0].url == "https://api.test/image?token=opaque"
    assert (
        result.answer
        == "![Original \\[diagram\\]](https://api.test/image?token=opaque)"
    )
