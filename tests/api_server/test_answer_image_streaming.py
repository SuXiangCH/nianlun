import pytest

from app.api_server.common.errors import ApiError


def test_safe_prefix_streams_text_before_image_and_freezes_following_text():
    from app.api_server.services.answer_image_streaming import ImageStreamPrefix

    stream = ImageStreamPrefix()
    assert stream.feed("说明\n\n!") == "说明\n\n"
    assert stream.feed("[图](nianlun-image:img_01)\n尾部\n") == ""
    final = "说明\n\n![可信图题](https://api.test/image)\n尾部\n"
    assert stream.remainder(final) == "![可信图题](https://api.test/image)\n尾部\n"


@pytest.mark.parametrize(
    "candidate",
    [
        "![ref][label]\n",
        "<img src='https://outside.test/x'>\n",
        "inline ![image](https://outside.test/x)\n",
        "[label]: https://outside.test/x\n",
        "```md\n![code](x)\n```\n",
    ],
)
def test_ambiguous_markdown_waits_for_final_projection(candidate):
    from app.api_server.services.answer_image_streaming import ImageStreamPrefix

    for split in range(len(candidate) + 1):
        stream = ImageStreamPrefix()
        assert stream.feed("Before\n") == "Before\n"
        assert stream.feed(candidate[:split]) + stream.feed(candidate[split:]) == ""
        assert stream.remainder("Before\n图片暂不可用") == "图片暂不可用"


def test_mismatching_committed_prefix_is_protocol_error():
    from app.api_server.services.answer_image_streaming import ImageStreamPrefix

    stream = ImageStreamPrefix()
    assert stream.feed("Sent\n") == "Sent\n"
    with pytest.raises(ApiError) as error:
        stream.remainder("Different\n")
    assert error.value.status_code == 502


def test_frozen_stream_counts_utf8_budget_without_unbounded_buffer():
    from app.api_server.services.answer_image_streaming import ImageStreamPrefix

    stream = ImageStreamPrefix(max_canonical_bytes=12)
    assert stream.feed("![x]") == ""
    assert stream.feed("中中") == ""
    with pytest.raises(ApiError) as error:
        stream.feed("中")
    assert error.value.status_code == 413


def test_transport_chunks_preserve_unicode_and_bound_encoded_bytes():
    from app.api_server.services.answer_image_streaming import utf8_chunks

    text = "中🙂a" * 10_000
    chunks = list(utf8_chunks(text))
    assert "".join(chunks) == text
    assert all(0 < len(chunk.encode("utf-8")) <= 16 * 1024 for chunk in chunks)
    assert len(chunks[0].encode("utf-8")) > 16 * 1024 - 4


@pytest.mark.parametrize("citation", ["[1]", "&#91;1&#93;", r"\[1\]"])
def test_auto_inserted_source_image_preserves_stream_prefix_at_every_split(citation):
    from app.api_server.services.answer_image_streaming import ImageStreamPrefix
    from nianlun.agent.answer_images import add_cited_source_images

    answer = f"Introduction\n\nFact {citation}\n\nSecond paragraph\n"
    committed = add_cited_source_images(answer, {1: [("img_01", "Source")]})
    for split in range(len(answer) + 1):
        stream = ImageStreamPrefix()
        sent = stream.feed(answer[:split]) + stream.feed(answer[split:])
        assert sent.startswith("Introduction\n")
        assert sent + stream.remainder(committed) == committed


@pytest.mark.parametrize(
    ("definition", "label"),
    [
        ("[pic]: nianlun-image:img_01\n", "pic"),
        ("> [pic]: nianlun-image:img_01\n", "pic"),
        (">> [pic]: nianlun-image:img_01\n", "pic"),
        ("- [pic]: nianlun-image:img_01\n", "pic"),
        ("+ [pic]: nianlun-image:img_01\n", "pic"),
        ("* [pic]: nianlun-image:img_01\n", "pic"),
        ("1. [pic]: nianlun-image:img_01\n", "pic"),
        ("2) [pic]: nianlun-image:img_01\n", "pic"),
        ("> - 1. > [pic]: nianlun-image:img_01\n", "pic"),
        ("- item\n\n  [pic]: nianlun-image:img_01\n", "pic"),
        ("[pic\n label]: nianlun-image:img_01\n", "pic label"),
        ("> [pic\n> label]: nianlun-image:img_01\n", "pic label"),
        ("- [pic]:\n    nianlun-image:img_01\n", "pic"),
    ],
)
@pytest.mark.parametrize("available", [False, True])
def test_reference_definitions_preserve_committed_prefix_at_every_split(
    definition, label, available
):
    from app.api_server.services.answer_image_streaming import ImageStreamPrefix
    from nianlun.agent.answer_images import normalize_answer_images

    answer = f"Introduction\n\n{definition}\n![{label}]\n"
    committed, _ = normalize_answer_images(
        answer, {"img_01": ("att", "Trusted caption")} if available else {}
    )
    assert "img_01" not in committed
    for split in range(len(answer) + 1):
        stream = ImageStreamPrefix()
        sent = stream.feed(answer[:split]) + stream.feed(answer[split:])
        assert sent.startswith("Introduction\n\n")
        assert "[pic" not in sent
        assert "nianlun-image:" not in sent
        assert sent + stream.remainder(committed) == committed


def test_ordinary_paragraphs_quotes_and_lists_continue_streaming():
    from app.api_server.services.answer_image_streaming import ImageStreamPrefix

    answer = (
        "Introduction\n\n> Quoted text\n\n- First item\n"
        "  continuation\n1. Ordered item\n> - Nested item\n"
        "Inline [label] text\n"
    )
    stream = ImageStreamPrefix()
    for line in answer.splitlines(keepends=True):
        assert stream.feed(line) == line
    assert stream.remainder(answer) == ""
