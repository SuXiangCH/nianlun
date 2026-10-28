import json
from dataclasses import replace

import pytest

from nianlun.agent.lead_agent.runner import RetrievalCollector
from nianlun.agent.image_context import AnswerImagePolicy


def payload(
    *,
    text="Before ![Original](secret/private.png) after",
    start=0,
    limit=None,
    doc="d1",
):
    syntax = "![Original](secret/private.png)"
    first = text.index(syntax)
    last = first + len(syntax)
    end = len(text) if limit is None else start + limit
    image = {
        "image_id": "img_" + "a" * 24,
        "generation": 3,
        "knowledge_base_id": "kb",
        "content_version": 8,
        "asset_relpath": "secret/archive.png",
        "sha256": "a" * 64,
        "size_bytes": 100,
        "media_type": "image/png",
        "width": 8,
        "height": 6,
        "orientation": 1,
        "alt_text": "Original",
        "source_start": first,
        "source_end": last,
    }
    return json.dumps(
        {
            "doc_id": doc,
            "doc_name": "Document",
            "line_spec": "1",
            "content": [
                {
                    "node_id": "n1",
                    "line_num": 1,
                    "text": text[start:end],
                    "char_offset": start,
                    "char_limit": limit,
                    "total_chars": len(text),
                    "next_char_offset": end if end < len(text) else None,
                    "text_truncated": end < len(text),
                    "images": [image] if first >= start and last <= end else [],
                    "image_spans": [
                        {"source_start": first, "source_end": last, "kind": "markdown"}
                    ],
                }
            ],
        }
    )


def test_v5_tool_projection_exposes_only_request_refs_and_keeps_source_pagination():
    collector = RetrievalCollector(tool_schema_version=5, image_capability=True)
    result = json.loads(collector.add_line_content_result(payload()))["content"][0]
    assert result["text"] == "Before ![Original](nianlun-image:img_01) after"
    assert result["offset_basis"] == "source_node"
    assert result["total_chars"] == len("Before ![Original](secret/private.png) after")
    assert result["images"][0]["ref_id"] == "img_01"
    assert (
        result["text"][
            result["images"][0]["text_start"] : result["images"][0]["text_end"]
        ]
        == "![Original](nianlun-image:img_01)"
    )
    assert "secret/" not in json.dumps(result)
    assert "sha256" not in json.dumps(result)
    assert (
        collector.snippets[0]["text"] == "Before ![Original](secret/private.png) after"
    )
    assert collector.snippets[0]["images"][0]["ref_id"] == "img_01"


def test_v4_preserves_old_source_text_without_new_image_fields():
    result = json.loads(
        RetrievalCollector(tool_schema_version=4).add_line_content_result(payload())
    )["content"][0]
    assert result["text"] == "Before ![Original](secret/private.png) after"
    assert "images" not in result
    assert "image_spans" not in result
    assert "offset_basis" not in result


def test_disabled_image_capability_masks_targets_and_exposes_no_directory():
    collector = RetrievalCollector(tool_schema_version=5, image_capability=False)
    result = json.loads(collector.add_line_content_result(payload()))["content"][0]
    assert "secret/" not in json.dumps(result)
    assert "images" not in result
    assert collector.image_context.candidates == {}


def test_partial_syntax_is_redacted_without_expanding_allowed_set():
    collector = RetrievalCollector(tool_schema_version=5, image_capability=True)
    result = json.loads(collector.add_line_content_result(payload(start=22, limit=6)))[
        "content"
    ][0]
    assert "secret" not in result["text"]
    assert "private" not in result["text"]
    assert result["next_char_offset"] == 28
    assert result.get("images", []) == []
    assert collector.image_context.candidates == {}


def test_request_refs_are_stable_and_full_identity_does_not_merge_other_documents():
    first = RetrievalCollector(tool_schema_version=5, image_capability=True)
    one = json.loads(first.add_line_content_result(payload()))["content"][0]
    repeated = json.loads(first.add_line_content_result(payload()))["content"][0]
    two = json.loads(first.add_line_content_result(payload(doc="d2")))["content"][0]
    other = RetrievalCollector(tool_schema_version=5, image_capability=True)
    isolated = json.loads(other.add_line_content_result(payload(doc="d3")))["content"][
        0
    ]
    assert (
        one["images"][0]["ref_id"]
        == repeated["images"][0]["ref_id"]
        == isolated["images"][0]["ref_id"]
        == "img_01"
    )
    assert two["images"][0]["ref_id"] == "img_02"
    assert len(first.snippets) == 2
    assert len(other.image_context.candidates) == 1


def test_selection_uses_batch_start_candidates_and_failed_calls_keep_previous_selection():
    collector = RetrievalCollector(tool_schema_version=5, image_capability=True)
    collector.add_line_content_result(payload())
    context = collector.image_context
    context.freeze_tool_batch([{"name": "select_answer_images", "id": "initial"}])
    context.select_answer_images("initial", ["img_01"])
    context.freeze_tool_batch(
        [
            {"name": "get_line_content", "id": "read"},
            {"name": "select_answer_images", "id": "same-batch"},
        ]
    )
    collector.add_line_content_result(payload(doc="d2"))
    with pytest.raises(ValueError, match="批次"):
        context.select_answer_images("same-batch", ["img_02"])
    assert [item["ref_id"] for item in context.selected_images] == ["img_01"]
    context.freeze_tool_batch([{"name": "select_answer_images", "id": "later"}])
    context.select_answer_images("later", ["img_02"])
    assert [item["ref_id"] for item in context.selected_images] == ["img_02"]


def test_multiple_selection_calls_in_batch_are_all_rejected():
    collector = RetrievalCollector(tool_schema_version=5, image_capability=True)
    collector.add_line_content_result(payload())
    context = collector.image_context
    context.freeze_tool_batch(
        [
            {"name": "select_answer_images", "id": "a"},
            {"name": "select_answer_images", "id": "b"},
        ]
    )
    for call_id in ["b", "a"]:
        with pytest.raises(ValueError, match="一次"):
            context.select_answer_images(call_id, ["img_01"])
    assert context.selected_images == []


@pytest.mark.parametrize(
    "refs", [[], ["img_01", "img_01"], ["unknown"], ["https://test/a.png"]]
)
def test_selection_rejects_invalid_sets_atomically(refs):
    collector = RetrievalCollector(tool_schema_version=5, image_capability=True)
    collector.add_line_content_result(payload())
    context = collector.image_context
    context.freeze_tool_batch([{"name": "select_answer_images", "id": "call"}])
    with pytest.raises(ValueError):
        context.select_answer_images("call", refs)
    assert context.selected_images == []


def test_selection_needs_preflight_and_applies_unique_resource_budget():
    policy = replace(AnswerImagePolicy(), max_answer_bytes=99)
    collector = RetrievalCollector(
        tool_schema_version=5, image_capability=True, image_policy=policy
    )
    collector.add_line_content_result(payload())
    context = collector.image_context
    with pytest.raises(ValueError, match="预检"):
        context.select_answer_images("unplanned", ["img_01"])
    context.freeze_tool_batch([{"name": "select_answer_images", "id": "planned"}])
    with pytest.raises(ValueError, match="预算"):
        context.select_answer_images("planned", ["img_01"])
    assert context.selected_images == []


def test_selection_checks_inline_and_per_file_budget_before_execution():
    for overrides in (
        {"inline_budget_enabled": True, "inline_max_bytes": 99},
        {"max_image_bytes": 99},
        {"max_image_pixels": 1},
    ):
        collector = RetrievalCollector(
            tool_schema_version=5,
            image_capability=True,
            image_policy=replace(AnswerImagePolicy(), **overrides),
        )
        collector.add_line_content_result(payload())
        context = collector.image_context
        context.freeze_tool_batch([{"name": "select_answer_images", "id": "plan"}])
        with pytest.raises(ValueError, match="预算"):
            context.select_answer_images("plan", ["img_01"])
        assert context.selected_images == []


def test_model_projection_exposes_frozen_limits_and_candidate_count():
    collector = RetrievalCollector(
        tool_schema_version=5,
        image_capability=True,
        image_policy=replace(AnswerImagePolicy(), max_answer_images=3),
    )
    content = json.loads(collector.add_line_content_result(payload()))["content"][0]
    assert content["image_candidates_total"] == 1
    assert '"max_answer_images":3' in content["image_budget"]
    assert "secret/" not in content["image_budget"]
