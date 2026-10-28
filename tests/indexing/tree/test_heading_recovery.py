from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from nianlun.indexing.tree.heading_recovery import (
    HeadingRecoveryConfig,
    recover_heading_levels,
    recover_heading_levels_with_llm,
)
from nianlun.indexing.tree.pipeline import build_md_index_sync


def _headings(*entries: tuple[str, int]) -> list[dict[str, int | str]]:
    return [
        {"title": title, "level": level, "line_num": index + 1}
        for index, (title, level) in enumerate(entries)
    ]


def test_recovers_arabic_outline_from_two_level_markdown():
    result = recover_heading_levels(
        _headings(
            ("报告标题", 1),
            ("1. 投资要点", 2),
            ("1.1 盈利预测", 2),
            ("1.1.1 收入端", 2),
            ("2. 风险提示", 2),
        )
    )

    assert [heading["level"] for heading in result.headings] == [1, 2, 3, 4, 2]
    assert result.mode == "rules"
    assert result.headings[2]["line_num"] == 3


def test_recovers_cjk_outline_and_keeps_source_text():
    source = _headings(("报告标题", 1), ("一、行业概况", 2), ("（一）市场规模", 2))
    result = recover_heading_levels(source)

    assert [heading["level"] for heading in result.headings] == [1, 2, 3]
    assert [heading["title"] for heading in result.headings] == [
        heading["title"] for heading in source
    ]


def test_missing_numbered_parent_does_not_create_a_deeper_level():
    result = recover_heading_levels(
        _headings(("报告标题", 1), ("1.1 孤立小节", 2), ("2. 后续章节", 2))
    )

    assert [heading["level"] for heading in result.headings] == [1, 2, 2]


def test_leaves_normal_two_level_headings_unchanged():
    source = _headings(("报告标题", 1), ("投资要点", 2), ("风险提示", 2))
    result = recover_heading_levels(source)

    assert result.headings == source
    assert result.mode == "source"


def test_rules_mode_feeds_recovered_levels_into_the_tree(tmp_path: Path):
    markdown_path = tmp_path / "mineru.pdf.md"
    markdown_path.write_text(
        "# 报告标题\n## 1. 章节\n## 1.1 小节\n正文。\n## 2. 其他章节\n",
        encoding="utf-8",
    )

    result = build_md_index_sync(
        str(markdown_path),
        add_node_summary=False,
        add_node_text=True,
        heading_recovery_mode="rules",
    )

    assert result["heading_recovery"]["mode"] == "rules"
    assert [node["title"] for node in result["structure"][0]["nodes"]] == [
        "1. 章节",
        "2. 其他章节",
    ]
    assert result["structure"][0]["nodes"][0]["nodes"][0]["title"] == "1.1 小节"


def test_pipeline_uses_dedicated_heading_recovery_llm(tmp_path: Path):
    markdown_path = tmp_path / "mineru.pdf.md"
    markdown_path.write_text(
        "# 报告标题\n## 章节\n## 小节\n## 风险提示\n",
        encoding="utf-8",
    )
    llm = _LLM('{"levels": {"0": 1, "1": 2, "2": 3, "3": 2}}')

    result = build_md_index_sync(
        str(markdown_path),
        add_node_summary=False,
        add_node_text=True,
        heading_recovery_mode="rules_then_llm",
        heading_recovery_llm=llm,
    )

    assert result["heading_recovery"]["mode"] == "rules_then_llm"
    assert result["structure"][0]["nodes"][0]["nodes"][0]["title"] == "小节"
    assert llm.prompts


@pytest.mark.parametrize("mode", ["rules", "rules_then_llm"])
@pytest.mark.parametrize("folding", [False, True])
def test_recovered_subtrees_fold_without_losing_text_or_image_references(
    tmp_path: Path, mode: str, folding: bool
):
    from nianlun.indexing.tree.pipeline import structure_to_list

    image = "![Original diagram](images/chart.png)"
    source = (
        "# Report\n"
        + "Report introduction.\n" * 100
        + "## 1. Chapter\nChapter text.\n"
        + f"## 1.1 Figure\n{image}\nFigure text.\n"
        + "## 2. Risks\n"
        + "Risk details.\n" * 100
    )
    markdown = tmp_path / "report.md"
    markdown.write_text(source, encoding="utf-8")
    llm = _LLM('{"levels": {"0": 1, "1": 2, "2": 3, "3": 2}}')
    result = build_md_index_sync(
        str(markdown),
        add_node_text=True,
        heading_recovery_mode=mode,
        heading_recovery_llm=llm,
        thin=folding,
        min_node_token=80,
    )
    root = result["structure"][0]
    chapter, risks = root["nodes"]
    assert [chapter["title"], risks["title"]] == ["1. Chapter", "2. Risks"]
    if folding:
        assert not chapter.get("nodes")
        assert image in chapter["text"]
        assert "Figure text." in chapter["text"]
    else:
        assert chapter["nodes"][0]["title"] == "1.1 Figure"
        assert image in chapter["nodes"][0]["text"]
    nodes = structure_to_list(result["structure"])
    assert [node["node_id"] for node in nodes] == [
        str(index).zfill(4) for index in range(3 if folding else 4)
    ]
    assert risks["line_num"] == source.split("\n").index("## 2. Risks") + 1
    assert sum(node["text"].count(image) for node in nodes) == 1
    assert markdown.read_text(encoding="utf-8") == source


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _LLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    async def ainvoke(self, prompt: str, **_kwargs: object) -> _Message:
        self.prompts.append(prompt)
        return _Message(self.response)


def test_llm_refines_unlocked_heading_and_preserves_rule_anchors():
    source = _headings(
        ("报告标题", 1),
        ("1. 投资要点", 2),
        ("盈利预测", 2),
        ("1.1 收入端", 2),
        ("2. 风险提示", 2),
    )
    llm = _LLM('{"levels": {"0": 1, "1": 2, "2": 3, "3": 3, "4": 2}}')

    result = asyncio.run(recover_heading_levels_with_llm(source, llm))

    assert [heading["level"] for heading in result.headings] == [1, 2, 3, 3, 2]
    assert result.mode == "rules_then_llm"
    assert "locked" in llm.prompts[0]


def test_llm_can_recover_unnumbered_two_level_headings():
    source = _headings(
        ("报告标题", 1),
        ("章节", 2),
        ("小节", 2),
        ("风险提示", 2),
    )
    llm = _LLM('{"levels": {"0": 1, "1": 2, "2": 3, "3": 2}}')

    result = asyncio.run(recover_heading_levels_with_llm(source, llm))

    assert [heading["level"] for heading in result.headings] == [1, 2, 3, 2]
    assert result.mode == "rules_then_llm"


def test_llm_invalid_response_records_reason_and_logs_without_heading_text(caplog):
    source = _headings(("报告标题", 1), ("章节", 2), ("小节", 2))
    llm = _LLM('{"levels": {"0": 1, "1": 2, "2": 5}}')

    with caplog.at_level(
        logging.WARNING, logger="nianlun.indexing.tree.heading_recovery"
    ):
        result = asyncio.run(recover_heading_levels_with_llm(source, llm))

    assert result.mode == "fallback"
    assert "llm_fallback=invalid_response:skipped_level" in result.diagnostics
    assert "llm_attempts=3" in result.diagnostics
    assert "reason=invalid_response:skipped_level" in caplog.text
    assert "heading recovery LLM fallback" in caplog.text
    assert "报告标题" not in caplog.text


def test_heading_recovery_uses_the_shared_llm_request_timeout():
    assert HeadingRecoveryConfig().llm_timeout_seconds == 600.0


def test_invalid_llm_result_falls_back_to_rules():
    source = _headings(
        ("报告标题", 1),
        ("1. 章节", 2),
        ("1.1 小节", 2),
        ("2. 其他章节", 2),
    )
    # The final primary numbered heading remains level 2 after rules, but is
    # still a locked outline anchor and cannot be promoted by the LLM.
    llm = _LLM('{"levels": {"0": 1, "1": 2, "2": 3, "3": 3}}')

    result = asyncio.run(recover_heading_levels_with_llm(source, llm))

    assert [heading["level"] for heading in result.headings] == [1, 2, 3, 2]
    assert result.mode == "fallback"
