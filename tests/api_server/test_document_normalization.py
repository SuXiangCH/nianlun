import json
from pathlib import Path

import pytest

from app.api_server.services.documents.normalization import (
    NormalizationResourceError,
    build_content_list_page_entries,
    normalize_chunk_resources,
)


def test_normalize_chunk_resources_rewrites_and_isolates_local_assets(
    tmp_path: Path,
) -> None:
    result = tmp_path / "result"
    (result / "images").mkdir(parents=True)
    (result / "files").mkdir()
    (result / "images/chart.png").write_bytes(b"chart")
    (result / "images/html.png").write_bytes(b"html")
    (result / "files/appendix.pdf").write_bytes(b"pdf")
    markdown = (
        "![chart](images/chart.png)\n"
        "[appendix][file]\n\n[file]: files/appendix.pdf\n"
        '<div><img src="images/html.png"></div>\n'
        "`![code](images/missing.png)`\n"
        "```md\n![fenced](images/missing.png)\n```\n"
        "[remote](https://example.com/value)\n"
    )

    normalized = normalize_chunk_resources(markdown, tmp_path, 2)

    assert "![chart](chunks/0002/assets/result/images/chart.png)" in (
        normalized.markdown
    )
    assert "[file]: chunks/0002/assets/result/files/appendix.pdf" in (
        normalized.markdown
    )
    assert 'src="chunks/0002/assets/result/images/html.png"' in normalized.markdown
    assert "`![code](images/missing.png)`" in normalized.markdown
    assert "![fenced](images/missing.png)" in normalized.markdown
    assert "https://example.com/value" in normalized.markdown
    assert normalized.assets == {
        "chunks/0002/assets/result/files/appendix.pdf": b"pdf",
        "chunks/0002/assets/result/images/chart.png": b"chart",
        "chunks/0002/assets/result/images/html.png": b"html",
    }


def test_normalize_chunk_resources_rejects_traversal_and_ambiguous_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "secret.png").write_bytes(b"secret")
    with pytest.raises(NormalizationResourceError, match="路径越界"):
        normalize_chunk_resources("![](../secret.png)", tmp_path / "result", 0)

    for folder in ("one", "two"):
        path = tmp_path / folder / "images"
        path.mkdir(parents=True)
        (path / "same.png").write_bytes(folder.encode())
    with pytest.raises(NormalizationResourceError, match="路径不唯一"):
        normalize_chunk_resources("![](images/same.png)", tmp_path, 0)


def test_content_list_page_entries_require_unambiguous_monotonic_blocks(
    tmp_path: Path,
) -> None:
    markdown = "# Heading\n\nfirst page\n\nsecond page"
    content_list = tmp_path / "content_list.json"
    content_list.write_text(
        json.dumps(
            [
                {"type": "text", "text": "Heading", "page_idx": 0},
                {"type": "text", "text": "first page", "page_idx": 0},
                {"type": "text", "text": "second page", "page_idx": 1},
            ]
        ),
        encoding="utf-8",
    )

    entries = build_content_list_page_entries(
        markdown,
        content_list,
        chunk_index=3,
        source_page_start=181,
        source_page_end=182,
        markdown_line_offset=10,
    )

    assert entries is not None
    assert [entry.model_dump() for entry in entries] == [
        {
            "markdown_line_start": 10,
            "markdown_line_end": 13,
            "chunk_index": 3,
            "source_page_start": 181,
            "source_page_end": 181,
            "evidence": "content_list",
        },
        {
            "markdown_line_start": 14,
            "markdown_line_end": 14,
            "chunk_index": 3,
            "source_page_start": 182,
            "source_page_end": 182,
            "evidence": "content_list",
        },
    ]

    content_list.write_text(
        json.dumps(
            [
                {"type": "text", "text": "second page", "page_idx": 1},
                {"type": "text", "text": "first page", "page_idx": 0},
            ]
        ),
        encoding="utf-8",
    )
    assert (
        build_content_list_page_entries(
            markdown,
            content_list,
            chunk_index=3,
            source_page_start=181,
            source_page_end=182,
            markdown_line_offset=10,
        )
        is None
    )
