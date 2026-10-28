from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api_server.config import ApiServerSettings
from app.api_server.main import create_app
from app.api_server.services.workspace_snapshot import publish_document_snapshot
from tests.api_server.test_image_catalog import png_bytes

NOW = "2026-10-26T00:00:00+00:00"


def publish(client, kb_id, workspace, *, generation=1, color="red", content=None):
    repository = client.app.state.services.documents.repository
    content = content or (
        '# Title\n\n![Diagram](a.png)\n\n<img src="a.png" alt="HTML diagram">\n\n'
        "![Reference][pic]\n\n[pic]: a.png\n\n![Missing](missing.png)\n\n"
        "![Remote](https://remote.test/private.png)\n\n![Alias](b.png)\n\n## Next heading\n"
    )
    raw = content.encode()
    source = "sources/doc.md"
    (workspace / "sources").mkdir(exist_ok=True)
    (workspace / source).write_bytes(raw)
    if generation == 1:
        repository.create_document(
            {
                "id": "doc",
                "knowledge_base_id": kb_id,
                "original_filename": "doc.md",
                "file_extension": ".md",
                "mime_type": "text/markdown",
                "size_bytes": len(raw),
                "source_relpath": source,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "parser": "native_markdown",
                "status": "uploaded",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
    else:
        repository.update_document("doc", {"pipeline_generation": generation})
    normalized = workspace / "artifacts" / "doc" / f"g{generation}" / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "a.png").write_bytes(png_bytes(color))
    (normalized / "b.png").write_bytes(png_bytes(color))
    version = publish_document_snapshot(
        repository,
        workspace=workspace,
        knowledge_base_id=kb_id,
        document_id="doc",
        generation=generation,
        content=raw,
        source_relpath=source,
        source_mime_type="text/markdown",
        idempotency_key=None,
        fts_enabled=False,
        now=NOW,
        tree_content=json.dumps(
            {"structure": [{"node_id": "node", "line_num": 1, "text": content}]}
        ).encode(),
    )
    return content, version


@pytest.fixture
def preview_case(tmp_path: Path):
    settings = ApiServerSettings(
        data_dir=tmp_path / "api",
        workspace_root=tmp_path / "workspaces",
        fts_enabled=False,
        vector_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        kb_id = client.post("/api/v1/knowledge-bases", json={"name": "Preview"}).json()[
            "data"
        ]["id"]
        workspace = settings.workspace_root / kb_id
        content, version = publish(client, kb_id, workspace)
        yield client, kb_id, workspace, content, version


def preview(client, kb_id):
    return client.get(f"/api/v1/knowledge-bases/{kb_id}/documents/doc/preview")


def test_preview_projects_all_image_syntax_without_exposing_storage(preview_case):
    client, kb_id, _, content, version = preview_case
    response = preview(client, kb_id)
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["content"] == content
    assert result["content_version"] == version
    rendered = result["rendered_content"]
    assert rendered.count("nianlun-document-image:") == 6
    assert "<img" not in rendered and "![Reference][pic]" not in rendered
    assert rendered.count("\n") == content.count("\n")
    image = next(item for item in result["images"] if item["status"] == "available")
    assert image["width"] == 8 and image["height"] == 6
    assert f"content_version={version}" in image["url"]
    assert "asset_relpath" not in response.text and "sha256" not in response.text
    received = client.get(image["url"])
    assert received.status_code == 200 and received.content == png_bytes()
    assert received.headers["content-type"] == "image/png"
    assert received.headers["x-content-type-options"] == "nosniff"
    assert received.headers["cache-control"] == "private, no-store"
    assert received.headers.get("x-request-id")
    unavailable = [item for item in result["images"] if item["status"] == "unavailable"]
    assert len(unavailable) == 2 and all(item["url"] is None for item in unavailable)
    assert rendered.count(f"nianlun-document-image:{image['id']}") == 4


def test_preview_image_url_keeps_its_original_version(preview_case):
    client, kb_id, workspace, _, _ = preview_case
    old = next(
        image
        for image in preview(client, kb_id).json()["data"]["images"]
        if image["status"] == "available"
    )
    publish(client, kb_id, workspace, generation=2, color="blue")
    current = next(
        image
        for image in preview(client, kb_id).json()["data"]["images"]
        if image["status"] == "available"
    )
    assert old["url"] != current["url"]
    assert client.get(old["url"]).content == png_bytes("red")
    assert client.get(current["url"]).content == png_bytes("blue")
    assert (
        client.get(
            current["url"].replace("content_version=2", "content_version=1")
        ).status_code
        == 404
    )


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_damaged_image_does_not_block_document_text(preview_case, damage):
    client, kb_id, workspace, content, _ = preview_case
    initial = preview(client, kb_id).json()["data"]
    image = next(item for item in initial["images"] if item["status"] == "available")
    asset = next((workspace / "artifacts/doc/g1/image_catalogs").rglob("*.png"))
    if damage == "missing":
        asset.unlink()
    else:
        asset.write_bytes(b"corrupt")
    result = preview(client, kb_id)
    assert result.status_code == 200
    assert result.json()["data"]["content"] == content
    assert client.get(image["url"]).status_code == 404


def test_preview_image_is_scoped_and_validates_version(preview_case):
    client, kb_id, _, _, _ = preview_case
    result = preview(client, kb_id).json()["data"]
    url = next(
        item["url"] for item in result["images"] if item["status"] == "available"
    )
    assert (
        client.get(url.replace("/documents/doc/", "/documents/other/")).status_code
        == 404
    )
    assert client.get(url.replace(kb_id, "other")).status_code == 404
    assert (
        client.get(url.replace("content_version=1", "content_version=999")).status_code
        == 404
    )
    assert (
        client.get(url.replace("content_version=1", "content_version=-1")).status_code
        == 422
    )
    assert client.get(url.split("?")[0]).status_code == 422


def test_text_only_document_remains_readable_without_image_catalog(preview_case):
    client, kb_id, workspace, _, _ = preview_case
    content, _ = publish(
        client, kb_id, workspace, generation=2, content="# Text only\n"
    )
    response = preview(client, kb_id)
    assert response.status_code == 200
    assert response.json()["data"]["images"] == []
    assert response.json()["data"]["rendered_content"] == content


def test_damaged_catalog_preserves_text_and_logs_the_degradation(preview_case, caplog):
    client, kb_id, workspace, content, _ = preview_case
    catalog = next((workspace / "artifacts/doc/g1/image_catalogs").rglob("images.json"))
    catalog.write_bytes(b"corrupt")
    response = preview(client, kb_id)
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["content"] == content
    assert all(image["status"] == "unavailable" for image in result["images"])
    assert "document_preview.catalog_unavailable" in caplog.text


def test_image_projection_preserves_heading_lines_and_code_blocks(preview_case):
    client, kb_id, workspace, _, _ = preview_case
    content = '# Heading\n\n![Multi\nline](a.png)\n\n<img\n src="a.png">\n\n```md\n![Code](a.png)\n```\n\n## Next\n'
    publish(client, kb_id, workspace, generation=2, content=content)
    result = preview(client, kb_id).json()["data"]
    assert result["rendered_content"].count("nianlun-document-image:") == 2
    assert "![Code](a.png)" in result["rendered_content"]
    assert result["rendered_content"].splitlines().index(
        "## Next"
    ) == content.splitlines().index("## Next")
