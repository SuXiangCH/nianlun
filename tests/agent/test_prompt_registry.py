from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from nianlun.agent.prompt_registry import (
    CURRENT_PROMPT_BUNDLE,
    LEGACY_PROMPT_BUNDLE,
    UnsupportedPromptBundle,
    resolve_prompt_bundle,
)
from nianlun.agent.prompts.v4 import lead, retrieval_answer
from nianlun.agent.prompts.v5 import retrieval_answer as current_retrieval_answer


class _KnowledgeBase:
    has_vector = False


def test_exact_legacy_release_selects_frozen_original_prompts():
    bundle = resolve_prompt_bundle(
        tool_schema_version=4,
        prompt_version=21,
        v1_prompt_hash="568b8426c403f98de5b4448c769289d0ad711e71fabd130c611f97446acae93a",
        checkpoint_schema_version=1,
    )

    assert bundle is LEGACY_PROMPT_BUNDLE
    assert bundle.lead_prompt_builder is lead.build_system_prompt
    assert bundle.answer_messages_builder is retrieval_answer.build_answer_messages
    assert hashlib.sha256(Path(retrieval_answer.__file__).read_bytes()).hexdigest() == (
        "568b8426c403f98de5b4448c769289d0ad711e71fabd130c611f97446acae93a"
    )
    assert hashlib.sha256(Path(lead.__file__).read_bytes()).hexdigest() == (
        "d4a1218298fd5be46ea65e8666cbc2df23288fef57ba40930bb280c12e915054"
    )
    assert "nianlun-image:" not in bundle.build_system_prompt(_KnowledgeBase())
    assert "[image:" not in bundle.build_answer_system_prompt()


def test_current_contract_selects_image_protocol_only_when_enabled():
    current = CURRENT_PROMPT_BUNDLE
    bundle = resolve_prompt_bundle(
        tool_schema_version=5,
        prompt_version=22,
        v1_prompt_hash=current.v1_prompt_hash,
        checkpoint_schema_version=1,
    )

    assert bundle is current
    for prompt in (
        bundle.build_system_prompt(_KnowledgeBase(), image_capability=True),
        bundle.build_answer_system_prompt(image_capability=True),
    ):
        assert "![图题](nianlun-image:img_01)" in prompt
        assert "顶层独立段落" in prompt
        assert "图片像素" in prompt
        assert "历史编号" in prompt
        assert "[image:" not in prompt
    for prompt in (
        bundle.build_system_prompt(_KnowledgeBase(), image_capability=True),
        bundle.build_retrieval_system_prompt(_KnowledgeBase(), image_capability=True),
    ):
        assert "select_answer_images" in prompt
        assert "只发图" in prompt
        assert "下一批" in prompt
    assert "select_answer_images" not in bundle.build_answer_system_prompt(
        image_capability=True
    )


@pytest.mark.parametrize("image_capability", [False, True])
def test_current_answer_messages_use_matching_prompt_branch(image_capability):
    bundle = CURRENT_PROMPT_BUNDLE
    messages, selected, omitted = bundle.build_answer_messages(
        "hello", [], context_window_tokens=16_000, image_capability=image_capability
    )

    assert messages[0]["content"] == bundle.build_answer_system_prompt(
        image_capability=image_capability
    )
    assert selected == omitted == []


def test_disabled_image_capability_has_no_selection_or_marker_instructions():
    bundle = CURRENT_PROMPT_BUNDLE
    for prompt in (
        bundle.build_system_prompt(_KnowledgeBase()),
        bundle.build_retrieval_system_prompt(_KnowledgeBase()),
        bundle.build_answer_system_prompt(),
    ):
        assert "select_answer_images" not in prompt
        assert "nianlun-image:" not in prompt
        assert "[image:" not in prompt
        assert "当前渠道不支持图片" in prompt


@pytest.mark.parametrize(
    "override",
    [
        {"v1_prompt_hash": "0" * 64},
        {"tool_schema_version": 3},
        {"tool_schema_version": 5},
        {"prompt_version": 22},
        {"checkpoint_schema_version": 2},
        {"checkpoint_schema_version": True},
        {"tool_schema_version": 4.0},
    ],
)
def test_unknown_full_contract_combinations_are_rejected(override):
    contract = {
        "tool_schema_version": 4,
        "prompt_version": 21,
        "v1_prompt_hash": LEGACY_PROMPT_BUNDLE.v1_prompt_hash,
        "checkpoint_schema_version": 1,
        **override,
    }

    with pytest.raises(UnsupportedPromptBundle):
        resolve_prompt_bundle(**contract)


def test_bundle_descriptor_cannot_be_mutated():
    with pytest.raises(FrozenInstanceError):
        CURRENT_PROMPT_BUNDLE.prompt_version = 999


def test_legacy_bundle_rejects_image_capability():
    with pytest.raises(UnsupportedPromptBundle, match="images"):
        LEGACY_PROMPT_BUNDLE.build_system_prompt(
            _KnowledgeBase(), image_capability=True
        )


def test_current_contract_hash_identifies_fixed_v1_resource():
    source = Path(current_retrieval_answer.__file__).read_bytes()
    assert hashlib.sha256(source).hexdigest() == CURRENT_PROMPT_BUNDLE.v1_prompt_hash


@pytest.mark.parametrize("bundle", [LEGACY_PROMPT_BUNDLE, CURRENT_PROMPT_BUNDLE])
def test_message_builders_preserve_question_and_optional_history(bundle):
    history = [{"role": "user", "content": "previous question"}]
    messages = bundle.build_retrieval_messages(
        "current question", conversation_context=history
    )
    assert messages[-1] == {"role": "user", "content": "本轮问题：\ncurrent question"}
    assert "previous question" in messages[0]["content"]

    answer_messages, selected, omitted = bundle.build_answer_messages(
        "current question", [], conversation_context=history
    )
    assert "previous question" in answer_messages[-1]["content"]
    assert "current question" in answer_messages[-1]["content"]
    assert selected == omitted == []


def test_registry_identity_is_stable_in_a_fresh_interpreter():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from nianlun.agent.prompt_registry import "
            "CURRENT_PROMPT_BUNDLE as c, LEGACY_PROMPT_BUNDLE as l; "
            "print(json.dumps([[b.bundle_id, *b.contract_key] for b in (l, c)]))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [
        [
            "nianlun-v4-p21-c1",
            4,
            21,
            "568b8426c403f98de5b4448c769289d0ad711e71fabd130c611f97446acae93a",
            1,
        ],
        [
            "nianlun-v5-p22-c1",
            5,
            22,
            "a4bb69ca9cb3cf4a1446269ad4c1ca68d7c4fb914aa3a1706caf44382328eb76",
            1,
        ],
    ]
