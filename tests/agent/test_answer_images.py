from nianlun.agent.answer_images import add_cited_source_images, normalize_answer_images
import pytest


def test_auto_images_follow_their_source_context_instead_of_the_first_citation():
    answer = (
        "行程概况：全程约 530 公里，车程约 8 小时。[1]\n\n"
        "当天游览大柴旦翡翠湖，随后入住大柴旦酒店休息。[1]\n\n"
        "高原反应通常两天后缓解，请自备药物并保证睡眠。[1]\n\n"
        "下一天行程 [2]"
    )
    result = add_cited_source_images(
        answer,
        {1: [("img_lake", "原文图片"), ("img_tips", "原文图片")]},
        source_contexts={
            (1, "img_lake"): "随后游览大柴旦翡翠湖，之后入住大柴旦酒店休息。",
            (1, "img_tips"): "高原反应症状通常两天后缓解，自备药物，保证睡眠。",
        },
    )
    assert (
        result.index("行程概况")
        < result.index("当天游览")
        < result.index("nianlun-image:img_lake")
    )
    assert (
        result.index("nianlun-image:img_lake")
        < result.index("高原反应")
        < result.index("nianlun-image:img_tips")
    )
    assert result.index("nianlun-image:img_tips") < result.index("下一天")


def test_auto_images_without_a_reliable_match_follow_the_last_source_citation():
    result = add_cited_source_images(
        "Overview [1]\n\nHotel details [1]\n\nNext source [2]",
        {1: [("img_01", "Original")]},
        source_contexts={(1, "img_01"): "Unrelated wording with no shared content"},
    )
    assert (
        result.index("Hotel details")
        < result.index("nianlun-image:img_01")
        < result.index("Next source")
    )


def test_source_context_uses_complete_list_and_table_boundaries():
    result = add_cited_source_images(
        "Overview [1]\n\n- Visit Emerald Lake [1]\n- Stay at the hotel\n\n"
        "| Advice |\n| --- |\n| Altitude sickness medicine [1] |\n\nNext",
        {1: [("img_lake", "Lake"), ("img_tips", "Advice")]},
        source_contexts={
            (1, "img_lake"): "Visit Emerald Lake",
            (1, "img_tips"): "Altitude sickness medicine",
        },
    )
    assert (
        result.index("Stay at the hotel")
        < result.index("nianlun-image:img_lake")
        < result.index("| Advice |")
    )
    assert (
        result.index("| Altitude sickness")
        < result.index("nianlun-image:img_tips")
        < result.index("Next")
    )


def test_auto_images_preserve_source_order_without_a_two_image_limit():
    images = [(f"img_{index}", f"Original {index}") for index in range(20)]
    result = add_cited_source_images("Answer [1]\n\nNext [1]", {1: images})
    normalized, occurrences = normalize_answer_images(
        result, {ref: (ref, caption) for ref, caption in images}
    )
    assert len(occurrences) == 20
    assert [item["attachment_id"] for item in occurrences] == [ref for ref, _ in images]
    assert (
        normalized.index("Next")
        < normalized.index("img_0")
        < normalized.index("img_19")
    )


def test_auto_images_obey_remaining_occurrence_and_body_budgets():
    answer = "Text [1]\n\n![Explicit](nianlun-image:img_first)"
    assert (
        add_cited_source_images(
            answer, {1: [("img_other", "Other")]}, max_occurrences=1
        )
        == answer
    )
    assert (
        add_cited_source_images(
            "Text [1]", {1: [("img_other", "Other")]}, max_canonical_bytes=20
        )
        == "Text [1]"
    )
    assert (
        add_cited_source_images("Text [1]", {1: [("img_other", "X" * 5000)]})
        == "Text [1]"
    )


def test_auto_images_do_not_treat_reference_links_as_citations():
    answer = "Read [1]\n\n[1]: https://example.test/"
    assert add_cited_source_images(answer, {1: [("img_01", "Original")]}) == answer


def test_only_standalone_logical_images_are_resolved():
    answer = (
        "说明 [1]\n\n![模型图题](nianlun-image:img_0123456789abcdef)\n\n"
        "```markdown\n![示例](nianlun-image:img_0123456789abcdef)\n```\n"
    )
    normalized, occurrences = normalize_answer_images(
        answer, {"img_0123456789abcdef": ("attachment-1", "原文 [图题]")}
    )
    assert "![原文 \\[图题\\]](nianlun-image:attachment-1)" in normalized
    assert "```markdown\n![示例](nianlun-image:img_0123456789abcdef)\n```" in normalized
    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert normalized[occurrence["start"] : occurrence["end"]] == (
        "![原文 \\[图题\\]](nianlun-image:attachment-1)"
    )


def test_unknown_external_and_embedded_images_never_become_resources():
    answer = (
        "![未知](nianlun-image:img_unknown)\n\n"
        "![外部](https://outside.test/private.png)\n\n"
        "文字 ![插图](nianlun-image:img_known) 继续\n"
    )
    normalized, occurrences = normalize_answer_images(
        answer, {"img_known": ("attachment-1", "图")}
    )
    assert "https://" not in normalized
    assert "nianlun-image:" not in normalized
    assert "文字" in normalized and "继续" in normalized
    assert not occurrences


def test_duplicate_images_keep_order_and_occurrence_identity():
    answer = "![一](nianlun-image:img_one)\n\n![二](nianlun-image:img_one)"
    normalized, occurrences = normalize_answer_images(
        answer, {"img_one": ("attachment-1", "图一")}
    )
    assert [item["attachment_id"] for item in occurrences] == [
        "attachment-1",
        "attachment-1",
    ]
    assert [item["kind"] for item in occurrences] == ["first", "repeat"]
    assert normalized.count("nianlun-image:attachment-1") == 2


def test_occurrence_limit_counts_every_image_in_one_paragraph():
    with pytest.raises(ValueError, match="引用次数"):
        normalize_answer_images(
            " ".join("![x](https://untrusted.test/image.png)" for _ in range(129)), {}
        )


def test_html_images_and_reference_targets_are_not_retained():
    answer = '<div><img src="https://untrusted.test/a.png" alt="x"></div>\n\n![x][photo]\n\n[photo]: https://untrusted.test/b.png\n'
    normalized, occurrences = normalize_answer_images(answer, {})
    assert "untrusted.test" not in normalized
    assert "<img" not in normalized
    assert not occurrences


def test_nested_images_are_replaced_without_changing_surrounding_markdown():
    answer = "> 前文 ![x](nianlun-image:img_01) 后文\n\n- ![x](https://bad.test/x)\n"
    normalized, occurrences = normalize_answer_images(answer, {"img_01": ("a", "图")})
    assert normalized == "> 前文 图片暂不可用 后文\n\n- 图片暂不可用\n"
    assert not occurrences


def test_code_math_links_and_unicode_line_separators_keep_semantics():
    answer = (
        "`![code](nianlun-image:img_01)`\n\n"
        "$![math](nianlun-image:img_01)$\n\n"
        "$$\n![display](nianlun-image:img_01)\n$$\n\n"
        "[ordinary](nianlun-image:img_01)\n\n"
        "前\u2028文字\r\n\r\n![x](nianlun-image:img_01)\r\n"
    )
    normalized, occurrences = normalize_answer_images(answer, {"img_01": ("a", "图")})
    assert normalized == answer.replace(
        "![x](nianlun-image:img_01)", "![图](nianlun-image:a)"
    )
    assert len(occurrences) == 1
    item = occurrences[0]
    assert normalized[item["start"] : item["end"]] == "![图](nianlun-image:a)"


def test_candidate_line_limit_is_utf8_bytes_and_all_output_is_bounded():
    with pytest.raises(ValueError, match="候选行"):
        normalize_answer_images(f"![{'图' * 1366}](nianlun-image:img_01)", {})
    with pytest.raises(ValueError, match="正文"):
        normalize_answer_images("a" * (1024 * 1024 + 1), {})


def test_srcset_only_html_images_and_images_in_tables_are_replaced():
    answer = '<img srcset="https://bad.test/1.png 1x, https://bad.test/2.png 2x">\n\n| Image |\n| --- |\n| ![x](nianlun-image:img_01) |\n'
    normalized, occurrences = normalize_answer_images(answer, {"img_01": ("a", "图")})
    assert "srcset" not in normalized and "bad.test" not in normalized
    assert "nianlun-image:" not in normalized
    assert "| 图片暂不可用 |" in normalized
    assert not occurrences


def test_ordinary_links_can_share_a_reference_definition_with_replaced_image():
    answer = "[ordinary][shared]\n\n![x][shared]\n\n[shared]: https://example.test/document\n"
    normalized, occurrences = normalize_answer_images(answer, {})
    assert "[ordinary][shared]" in normalized
    assert "[shared]: https://example.test/document" in normalized
    assert "![x]" not in normalized
    assert not occurrences


def test_unsafe_scheme_is_removed_and_fallback_is_localizable():
    normalized, occurrences = normalize_answer_images(
        "![unsafe](data:text/html;base64,xxx)", {}, unavailable_text="Image unavailable"
    )
    assert normalized == "Image unavailable"
    assert not occurrences


@pytest.mark.parametrize("shared", [False, True])
def test_reference_definition_line_is_included_in_image_line_budget(shared):
    answer = (
        ("[ordinary][r]\n\n" if shared else "")
        + "![x][r]\n\n[r]: https://example.test/"
        + "x" * 5000
    )
    with pytest.raises(ValueError, match="候选行"):
        normalize_answer_images(answer, {})


def test_html_replacement_does_not_reveal_new_image_authorization():
    answer = '<img src="https://evil.test/1">\n![x](https://evil.test/2)\n![x](nianlun-image:attachment-1)'
    normalized, occurrences = normalize_answer_images(
        answer, {"img_01": ("attachment-1", "图")}
    )
    assert "evil.test" not in normalized
    assert "nianlun-image:" not in normalized
    assert not occurrences


def test_html_exposed_images_are_still_counted_towards_occurrence_limit():
    with pytest.raises(ValueError, match="引用次数"):
        normalize_answer_images(
            '<img src="https://evil.test/1">\n'
            + "\n".join("![x](https://evil.test/2)" for _ in range(129)),
            {},
        )


def test_table_cell_image_cannot_hide_in_cross_cell_backticks():
    normalized, occurrences = normalize_answer_images(
        "| `code | ![x](https://evil.test/x)` |\n| --- | --- |", {}
    )
    assert "evil.test" not in normalized
    assert not occurrences


def test_trusted_caption_expansion_is_bounded_before_canonical_commit():
    with pytest.raises(ValueError, match="候选行"):
        normalize_answer_images(
            "![x](nianlun-image:img_01)", {"img_01": ("attachment-1", "x" * 5000)}
        )


def test_final_closure_preserves_existing_authorized_occurrence_offsets():
    answer = '![ok](nianlun-image:img_01)\n\n<img src="https://evil.test/1">\n![bad](https://evil.test/2)'
    normalized, occurrences = normalize_answer_images(
        answer, {"img_01": ("attachment-1", "图")}
    )
    assert "evil.test" not in normalized
    assert len(occurrences) == 1
    item = occurrences[0]
    assert (
        normalized[item["start"] : item["end"]] == "![图](nianlun-image:attachment-1)"
    )


@pytest.mark.parametrize(
    "html",
    [
        '<image src="https://evil.test/x">',
        '<svg><image href="https://evil.test/x"/></svg>',
        '<svg><feImage xlink:href="https://evil.test/x"/></svg>',
        '<picture><source srcset="https://evil.test/x"></picture>',
        '<input type="image" src="https://evil.test/x">',
        '<video poster="https://evil.test/x"></video>',
        '<math><mglyph src="https://evil.test/x"></math>',
        '<object data="https://evil.test/x" type="image/png"></object>',
        '<embed src="https://evil.test/x" type="image/png">',
        '<div style="background-image:url(https://evil.test/x)"></div>',
        '<link rel="preload" as="image" href="https://evil.test/x">',
        '<table background="https://evil.test/x"><tr><td>Text</td></tr></table>',
    ],
)
def test_unsupported_html_cannot_embed_or_load_images(html):
    normalized, occurrences = normalize_answer_images(html, {})
    assert "evil.test" not in normalized
    assert "图片暂不可用" in normalized
    assert not occurrences


def test_safe_html_table_and_ordinary_links_are_preserved():
    answer = '<table><tr><td colspan="2">Value</td></tr></table>\n\n<a href="https://example.test/page">page</a>'
    normalized, occurrences = normalize_answer_images(answer, {})
    assert normalized == answer
    assert not occurrences
