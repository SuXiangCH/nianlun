from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from app.api_server.config import ApiServerSettings
from nianlun.agent.image_context import AnswerImagePolicy
from tests.api_server.test_answer_image_service import image_context as image_context
from tests.api_server.test_answer_image_service import save_result


@pytest.mark.parametrize(
    "name",
    [
        "max_image_bytes",
        "max_image_pixels",
        "max_candidates",
        "max_answer_images",
        "max_answer_bytes",
        "max_preview_images",
        "max_preview_bytes",
        "max_occurrences",
        "max_candidate_line_bytes",
        "max_canonical_bytes",
        "inline_max_bytes",
        "inline_total_bytes",
        "inline_response_max_bytes",
    ],
)
def test_image_policy_settings_validate_strict_positive_limits(name):
    for value in (0, -1, True, 1.5, "2"):
        with pytest.raises(ValueError):
            ApiServerSettings(**{"answer_image_" + name: value})


def test_image_policy_environment_and_request_freeze(monkeypatch):
    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_MAX_ANSWER_IMAGES", "5")
    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_MAX_IMAGE_BYTES", "1234")
    settings = ApiServerSettings.from_env()
    policy = settings.freeze_image_policy(transport="data_url")
    assert policy.max_answer_images == 5
    assert policy.max_image_bytes == 1234
    assert policy.inline_budget_enabled is True
    assert settings.freeze_image_policy().inline_budget_enabled is False
    with pytest.raises(FrozenInstanceError):
        policy.max_answer_images = 99


def test_inline_over_budget_does_not_archive_or_read_source(image_context, monkeypatch):
    context = image_context
    policy = replace(
        AnswerImagePolicy(), inline_max_bytes=1, inline_budget_enabled=True
    )

    def unexpected(*args, **kwargs):
        pytest.fail("over-budget image must be rejected before archive IO")

    monkeypatch.setattr(context.service, "_archive_candidate", unexpected)
    result = context.service.prepare_result("app-1", context.result, policy=policy)
    assert result["images"][0]["status"] == "unavailable"
    assert result["images"][0]["unavailable_reason"] == "inline_budget_exceeded"
    assert "nianlun-image:" not in result["answer"]


def test_frozen_policy_controls_validation_and_output_bounds(image_context):
    context = image_context
    result = context.service.prepare_result(
        "app-1",
        context.result,
        policy=replace(AnswerImagePolicy(), max_image_pixels=1),
    )
    assert result["images"][0]["unavailable_reason"] == "pixel_limit_exceeded"
    with pytest.raises(ValueError, match="大小|限制"):
        context.service.prepare_result(
            "app-1",
            context.result,
            policy=replace(AnswerImagePolicy(), max_canonical_bytes=5),
        )


def test_preview_rejections_are_counted_and_do_not_use_answer_capacity(image_context):
    context = image_context
    source = deepcopy(context.result)
    source["answer"] = "Text only"
    source["retrieved_snippets"].append(deepcopy(source["retrieved_snippets"][0]))
    second = source["retrieved_snippets"][-1]
    second["images"][0].update(image_id="img_other", sha256="b" * 64)
    result = context.service.prepare_result(
        "app-1",
        source,
        policy=replace(AnswerImagePolicy(), max_preview_images=1),
    )
    assert len(result["images"]) == 1
    assert result["images"][0]["display_in_answer"] is False
    assert result["image_previews_unavailable"] == 1
    save_result(context, result)
    messages = context.chat.get_messages("app-1", "conversation-1")
    assert messages[-1]["image_previews_unavailable"] == 1


def test_old_message_schema_receives_preview_count_without_losing_history(
    image_context,
):
    from app.api_server.database import initialize_database

    context = image_context
    connection = context.factory.connect()
    try:
        connection.execute(
            "ALTER TABLE messages DROP COLUMN image_previews_unavailable"
        )
        connection.commit()
    finally:
        connection.close()
    initialize_database(context.factory)
    initialize_database(context.factory)
    messages = context.chat.get_messages("app-1", "conversation-1")
    assert len(messages) == 2
    assert all(message["image_previews_unavailable"] == 0 for message in messages)


def test_resource_deduplication_preserves_all_private_source_bindings(image_context):
    context = image_context
    source = deepcopy(context.result)
    original = source["retrieved_snippets"][0]
    for citation in range(2, 7):
        item = deepcopy(original)
        item.update(citation_id=citation, node_id=f"node-{citation}")
        source["retrieved_snippets"].append(item)
    result = context.service.prepare_result("app-1", source, policy=AnswerImagePolicy())
    image = result["images"][0]
    assert len(image["source_bindings"]) == 6


@pytest.mark.parametrize("failure", ["budget", "inline", "archive"])
def test_preview_alias_failures_count_one_resource(image_context, monkeypatch, failure):
    context = image_context
    source = deepcopy(context.result)
    source["answer"] = "Text only"
    original = source["retrieved_snippets"][0]
    alias = deepcopy(original)
    alias.update(doc_id="other-doc", citation_id=2)
    alias["images"][0].update(image_id="img_alias", ref_id="img_02")
    source["retrieved_snippets"].append(alias)
    attempts = []

    def unavailable(candidate, ref_id, **kwargs):
        attempts.append(ref_id)
        return context.service._unavailable_candidate(candidate, ref_id)

    monkeypatch.setattr(context.service, "_archive_candidate", unavailable)
    policy = AnswerImagePolicy()
    if failure == "budget":
        policy = replace(policy, max_preview_bytes=1)
    elif failure == "inline":
        policy = replace(policy, inline_budget_enabled=True)
    result = context.service.prepare_result("app-1", source, policy=policy)
    assert result["image_previews_unavailable"] == 1
    assert len(attempts) == (1 if failure == "archive" else 0)
    assert result["images"] == []
