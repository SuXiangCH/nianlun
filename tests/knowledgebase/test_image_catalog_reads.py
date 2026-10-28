from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

import pytest

from nianlun.knowledgebase import KnowledgeBase
from nianlun.knowledgebase.workspace_view import WorkspaceViewError


def _make_snapshot(tmp_path: Path, version: int = 1):
    prefix = f"artifacts/g{version}"
    assets = {}
    entries = []
    nodes = []
    for node_index in range(10):
        text = "body\n"
        node_id = f"node-{node_index}"
        for image_index in range(2):
            image_id = f"image-{node_index}-{image_index}"
            asset = f"{prefix}/assets/{image_id}.bin"
            raw = f"asset-{image_id}-v{version}".encode()
            assets[asset] = raw
            syntax = f"![figure]({image_id}.png)"
            entries.append(
                {
                    "image_id": image_id,
                    "asset_relpath": asset,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                    "occurrences": [
                        {
                            "node_id": node_id,
                            "char_offset": len(text),
                            "char_end": len(text) + len(syntax),
                        }
                    ],
                }
            )
            text += syntax + "\n"
        nodes.append({"node_id": node_id, "line_num": node_index + 1, "text": text})
    catalog_path = f"{prefix}/images.json"
    tree_path = f"{prefix}/tree.json"
    artifacts = {
        **assets,
        catalog_path: json.dumps(
            {
                "format_version": 1,
                "document_id": "doc-1",
                "generation": version,
                "entries": entries,
            }
        ).encode(),
        tree_path: json.dumps({"structure": nodes}).encode(),
    }
    for relpath, raw in artifacts.items():
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    manifest = {
        "schema_version": 3,
        "knowledge_base_id": "kb-1",
        "content_version": version,
        "documents": [
            {
                "document_id": "doc-1",
                "generation": version,
                "index_relpath": tree_path,
                "image_catalog_relpath": catalog_path,
            }
        ],
        "artifact_files": [
            {
                "relpath": relpath,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for relpath, raw in artifacts.items()
        ],
    }
    manifest_raw = json.dumps(manifest).encode()
    snapshot = f"snapshots/r{version}"
    (tmp_path / snapshot).mkdir(parents=True)
    (tmp_path / snapshot / "manifest.json").write_bytes(manifest_raw)
    kb = KnowledgeBase(
        tmp_path,
        snapshot_relpath=snapshot,
        snapshot_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        knowledge_base_id="kb-1",
        content_version=version,
    )
    return kb, catalog_path, assets


@pytest.mark.parametrize("char_limit", [None, 1])
def test_line_content_verifies_catalog_and_assets_once_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, char_limit: int | None
):
    kb, catalog_path, assets = _make_snapshot(tmp_path)
    reads: Counter[Path] = Counter()
    original = Path.read_bytes

    def counted_read(path: Path) -> bytes:
        reads[path] += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    result = json.loads(kb.get_line_content("doc-1", "1-10", char_limit=char_limit))

    assert result["matches"] == 10
    assert [len(item["images"]) for item in result["content"]] == [
        2 if char_limit is None else 0
    ] * 10
    assert reads[tmp_path / catalog_path] == 1
    assert {reads[tmp_path / asset] for asset in assets} == {1}


def test_line_content_without_matches_does_not_read_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    kb, catalog_path, assets = _make_snapshot(tmp_path)
    original = Path.read_bytes

    def checked_read(path: Path) -> bytes:
        assert path != tmp_path / catalog_path
        assert path not in {tmp_path / asset for asset in assets}
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", checked_read)
    assert json.loads(kb.get_line_content("doc-1", "99"))["matches"] == 0


@pytest.mark.parametrize("change_size", [False, True])
def test_line_content_revalidates_assets_between_calls(
    tmp_path: Path, change_size: bool
):
    kb, _, assets = _make_snapshot(tmp_path)
    kb.get_line_content("doc-1", "1-10")
    asset, raw = next(iter(assets.items()))
    (tmp_path / asset).write_bytes(b"x" if change_size else b"x" * len(raw))

    with pytest.raises(WorkspaceViewError, match="hash"):
        kb.get_line_content("doc-1", "10", char_limit=1)


def test_line_content_keeps_catalog_reads_bound_to_snapshot_and_call(tmp_path: Path):
    first, _, _ = _make_snapshot(tmp_path)
    second, _, _ = _make_snapshot(tmp_path, version=2)
    first_result = json.loads(first.get_line_content("doc-1", "1"))
    first_result["content"][0]["images"][0]["asset_relpath"] = "changed"
    catalog = first._workspace_view.read_image_catalog("doc-1")
    assert catalog is not None
    catalog["entries"][0]["asset_relpath"] = "changed"

    for kb, version in ((second, 2), (first, 1)):
        image = json.loads(kb.get_line_content("doc-1", "1"))["content"][0]["images"][0]
        assert image["asset_relpath"].startswith(f"artifacts/g{version}/")
        assert image["generation"] == version
        assert image["content_version"] == version
