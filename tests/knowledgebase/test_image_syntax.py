import pytest

from nianlun.knowledgebase.image_syntax import parse_image_syntax


@pytest.mark.parametrize(
    "source,syntax",
    [
        ("正文\n\n![中文图](assets/a.png)\n", "![中文图](assets/a.png)"),
        (
            '# T\r\n\r\n![中文\r\n图](<assets/a.png> "title")\r\n',
            '![中文\r\n图](<assets/a.png> "title")',
        ),
        ("> before\n> ![图](assets/a.png)\n", "![图](assets/a.png)"),
        ("- item ![图](assets/a.png)\n", "![图](assets/a.png)"),
        ("![原图][figure]\n\n[figure]: assets/a.png\n", "![原图][figure]"),
    ],
)
def test_image_ranges_cover_whole_original_syntax(source, syntax):
    result = parse_image_syntax(source)
    assert len(result.images) == 1
    image = result.images[0]
    assert source[image.start : image.end] == syntax
    assert image.source == "assets/a.png"


def test_repeated_images_have_distinct_ranges_and_code_stays_literal():
    source = "![a](a.png) ![a](a.png)\n\n`![a](a.png)`\n\n```md\n![a](a.png)\n```"
    images = parse_image_syntax(source).images
    assert [(item.start, item.end) for item in images] == [(0, 11), (12, 23)]


def test_reference_definitions_outside_node_can_resolve_images():
    result = parse_image_syntax(
        "![caption][pic]", reference_markdown="[pic]: assets/a.png"
    )
    assert result.images[0].source == "assets/a.png"
    assert result.hidden == ()


def test_reference_definitions_and_html_images_are_redacted_by_source_range():
    source = '![caption][pic]\n\n[pic]: secret/a.png\n\n<p>text<img src="secret/b.png" alt="B"></p>\n'
    result = parse_image_syntax(source)
    assert [image.source for image in result.images] == ["secret/a.png", "secret/b.png"]
    hidden = [source[span.start : span.end] for span in result.hidden]
    assert any("[pic]: secret/a.png" in item for item in hidden)
    assert any('<img src="secret/b.png" alt="B">' in item for item in hidden)


@pytest.mark.parametrize(
    "source,syntax",
    [
        ("before\x00 ![a](assets/a.png)", "![a](assets/a.png)"),
        ("![a\x00b](assets/a.png)", "![a\x00b](assets/a.png)"),
        ("before\r\r![a\rb](assets/a.png)\r", "![a\rb](assets/a.png)"),
        ("before\vtext ![a](assets/a.png)", "![a](assets/a.png)"),
        ("- a\n\n\t![a\n\tb](assets/a.png)", "![a\n\tb](assets/a.png)"),
        ("> - a\r\n>\r\n>\t![a\r\n>\t b](assets/a.png)", "![a\r\n>\t b](assets/a.png)"),
        ("# before\x00 ![a](assets/a.png) #", "![a](assets/a.png)"),
        ("before\x00 ![a](assets/a.png)\n===", "![a](assets/a.png)"),
    ],
)
def test_normalized_inline_content_preserves_original_ranges(source, syntax):
    result = parse_image_syntax(source)
    assert len(result.images) == 1
    image = result.images[0]
    assert source[image.start : image.end] == syntax
    assert image.start == source.index("![")
    assert image.end == source.index("assets/a.png)") + len("assets/a.png)")


def test_normalized_html_and_reference_ranges_are_complete():
    source = '> ![a\x00][pic]\r>\r> [pic]: secret/a.png\r>\r> <img\r> src="secret/b.png" alt="b\x00">\r'
    result = parse_image_syntax(source)
    assert [source[item.start : item.end] for item in result.images] == [
        "![a\x00][pic]",
        '<img\r> src="secret/b.png" alt="b\x00">',
    ]
    hidden = [source[item.start : item.end] for item in result.hidden]
    assert "> [pic]: secret/a.png\r" in hidden
    assert '<img\r> src="secret/b.png" alt="b\x00">' in hidden


def test_normalization_does_not_authorize_code_or_reject_plain_text():
    assert parse_image_syntax("plain\x00text\rnext\vline").images == ()
    source = "\t![a](secret/a.png)\r\n\r\n```\r![b](secret/b.png)\r```"
    assert parse_image_syntax(source).images == ()


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
def test_multiline_reference_definition_and_repeated_images(separator):
    definition = f'[pic]:{separator}  <secret/a.png>{separator}  "title"{separator}'
    source = f"![a][pic] ![a][pic]{separator}{separator}{definition}"
    result = parse_image_syntax(source)
    assert [(item.start, item.end) for item in result.images] == [(0, 9), (10, 19)]
    assert [source[item.start : item.end] for item in result.hidden] == [definition]


@pytest.mark.parametrize("prefix", ["", "> ", "> - "])
def test_html_block_coordinates_follow_block_extraction(prefix):
    continuation = ">   " if prefix == "> - " else prefix
    source = (
        prefix
        + "<div>\r\n"
        + continuation
        + '<img src="secret/a.png"\r\n'
        + continuation
        + ' alt="a\x00">\r\n'
        + continuation
        + "</div>"
    )
    result = parse_image_syntax(source)
    assert len(result.images) == 1
    item = result.images[0]
    assert (
        source[item.start : item.end]
        == '<img src="secret/a.png"\r\n' + continuation + ' alt="a\x00">'
    )
    assert result.hidden[0].start == item.start
    assert result.hidden[0].end == item.end


def test_html_coordinates_do_not_treat_unicode_separators_as_newlines():
    source = 'before\vtext <img src="secret/a.png"> and <img src="secret/b.png">'
    result = parse_image_syntax(source)
    assert [source[item.start : item.end] for item in result.images] == [
        '<img src="secret/a.png">',
        '<img src="secret/b.png">',
    ]


@pytest.mark.parametrize("separator", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["", "> "])
def test_table_cells_preserve_image_provenance_with_escaped_pipes(separator, prefix):
    from markdown_it import MarkdownIt

    source = separator.join(
        prefix + line
        for line in [
            "| A | B |",
            "| --- | --- |",
            r"| escaped\|pipe | ![cap\|tion](https://secret.test/a) |",
            "| empty |",
        ]
    )
    result = parse_image_syntax(
        source, parser=MarkdownIt("commonmark", {"html": True}).enable("table")
    )
    assert len(result.images) == 1
    image = result.images[0]
    assert source[image.start : image.end] == r"![cap\|tion](https://secret.test/a)"


@pytest.mark.parametrize("explicit_parser", [False, True])
def test_table_cell_code_does_not_hide_image_in_another_cell(explicit_parser):
    from markdown_it import MarkdownIt

    source = "| `code | ![x](https://secret.test/x)` |\n| --- | --- |"
    result = parse_image_syntax(
        source,
        **(
            {"parser": MarkdownIt("commonmark").enable("table")}
            if explicit_parser
            else {}
        ),
    )
    assert len(result.images) == 1
    assert (
        source[result.images[0].start : result.images[0].end]
        == "![x](https://secret.test/x)"
    )
