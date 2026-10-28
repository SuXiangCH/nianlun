from io import BytesIO
from pathlib import Path
import json

from nianlun.knowledgebase import KnowledgeBase

from PIL import Image

from app.api_server.services.documents.image_catalog import (
    build_image_catalog,
    stage_image_catalog,
)


def png_bytes(color: str = "red") -> bytes:
    output = BytesIO()
    with Image.new("RGB", (8, 6), color) as image:
        image.save(output, format="PNG")
    return output.getvalue()


def test_catalog_validates_and_stages_normalized_images(tmp_path: Path):
    root = tmp_path / "kb"
    normalized = root / "artifacts" / "doc-1" / "g1" / "normalized"
    asset = normalized / "chunks" / "0000" / "assets" / "diagram.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(png_bytes())
    markdown = b"# Title\n\n![Architecture](chunks/0000/assets/diagram.png)\n"
    tree = {
        "structure": [
            {
                "node_id": "0000",
                "line_num": 1,
                "text": "# Title\n\n![Architecture](chunks/0000/assets/diagram.png)",
            }
        ]
    }

    built = build_image_catalog(root, "doc-1", 1, markdown, tree)
    assert built.catalog is not None
    assert len(built.catalog.entries) == 1
    entry = built.catalog.entries[0]
    assert entry.media_type == "image/png"
    assert entry.occurrences[0].node_id == "0000"
    assert entry.occurrences[0].char_offset is not None
    stage_image_catalog(root, built.files)
    assert (root / entry.asset_relpath).read_bytes() == asset.read_bytes()
    assert (
        root / entry.asset_relpath.rsplit("/assets/", 1)[0] / "images.json"
    ).is_file()


def test_catalog_rejects_external_and_missing_images_without_publishing(tmp_path: Path):
    root = tmp_path / "kb"
    normalized = root / "artifacts" / "doc-1" / "g1" / "normalized"
    normalized.mkdir(parents=True)
    markdown = b"![remote](https://example.test/a.png)\n![missing](missing.png)"
    built = build_image_catalog(root, "doc-1", 1, markdown, {"structure": []})
    assert built.catalog is None
    assert built.files == {}


def test_catalog_uses_configured_file_and_pixel_limits(tmp_path: Path):
    normalized = tmp_path / "artifacts" / "doc" / "g1" / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "a.png").write_bytes(png_bytes())
    markdown = b"![original](a.png)"
    for options in ({"max_bytes": 1}, {"max_pixels": 1}):
        built = build_image_catalog(tmp_path, "doc", 1, markdown, {}, **options)
        assert built.catalog is None
        assert built.files == {}


def test_catalog_ranges_use_sanitized_node_syntax_and_keep_repeated_occurrences(
    tmp_path: Path,
):
    normalized = tmp_path / "artifacts" / "doc-1" / "g1" / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "a.png").write_bytes(png_bytes())
    text = "# 标题\r\n\r\n正文\ud800\r\n![中文\r\n图](a.png)\r\n\r\n![再看][pic]\r\n\r\n[pic]: a.png"
    tree = {"structure": [{"node_id": "n1", "line_num": 1, "text": text}]}
    markdown = text.encode("utf-8", errors="replace")
    built = build_image_catalog(tmp_path, "doc-1", 1, markdown, tree)
    entry = built.catalog.entries[0]
    sanitized = markdown.decode()
    assert [
        sanitized[item.char_offset : item.char_end] for item in entry.occurrences
    ] == [
        "![中文\r\n图](a.png)",
        "![再看][pic]",
    ]
    assert [item.line_num for item in entry.occurrences] == [4, 7]


def test_knowledge_reads_only_complete_image_syntax_in_source_window(tmp_path: Path):
    normalized = tmp_path / "artifacts" / "doc-1" / "g1" / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "a.png").write_bytes(png_bytes())
    text = "# 标题\n\n前文\n![原图](a.png)\n\n后文"
    tree = {
        "doc_name": "中文",
        "structure": [{"node_id": "n1", "line_num": 1, "text": text}],
    }
    built = build_image_catalog(tmp_path, "doc-1", 1, text.encode(), tree)
    stage_image_catalog(tmp_path, built.files)
    catalog_path = next(path for path in built.files if path.endswith("/images.json"))
    (tmp_path / "doc-1.json").write_text(json.dumps(tree), encoding="utf-8")
    (tmp_path / "_meta.json").write_text(
        json.dumps(
            {
                "doc-1": {
                    "doc_name": "中文",
                    "line_count": 7,
                    "image_catalog_path": catalog_path,
                    "generation": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    kb = KnowledgeBase(tmp_path)
    partial = json.loads(
        kb.get_line_content("doc-1", "1", char_offset=text.index("a.png"), char_limit=5)
    )["content"][0]
    assert partial["images"] == []
    complete = json.loads(
        kb.get_line_content(
            "doc-1", "1", char_offset=text.index("!["), char_limit=len("![原图](a.png)")
        )
    )["content"][0]
    assert len(complete["images"]) == 1
    assert complete["images"][0]["source_start"] == text.index("![")
    assert complete["images"][0]["source_end"] == text.index("![") + len(
        "![原图](a.png)"
    )
    assert complete["text"] == "![原图](a.png)"
