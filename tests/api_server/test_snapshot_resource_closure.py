import pytest
import hashlib
import json

from app.api_server.services import workspace_snapshot as snapshots
from app.api_server.services.documents.image_catalog import build_image_catalog
from tests.api_server.test_image_catalog import png_bytes
from tests.api_server.test_workspace_snapshot import (
    NOW,
    _repository,
    _seed_knowledge_base,
)


def test_manifest_requires_catalog_record():
    entry = snapshots.SnapshotDocumentEntry(
        document_id="a",
        generation=1,
        index_relpath="tree.json",
        image_catalog_relpath="images.json",
    )
    record = snapshots.SnapshotArtifactFile(
        relpath="tree.json", size_bytes=0, sha256="0" * 64
    )
    with pytest.raises(snapshots.SnapshotError, match="images.json"):
        snapshots.build_manifest("kb", 1, [entry], [record])


def test_digest_includes_unavailable_identity(tmp_path):
    normalized = tmp_path / "artifacts/a/g1/normalized"
    normalized.mkdir(parents=True)
    (normalized / "a.png").write_bytes(png_bytes())
    first = build_image_catalog(tmp_path, "a", 1, b"![](a.png) ![](missing.png)", {})
    second = build_image_catalog(tmp_path, "a", 1, b"![](a.png) ![](other.png)", {})
    assert first.catalog.bundle_digest != second.catalog.bundle_digest


@pytest.mark.parametrize(
    "damage",
    [
        "asset_corrupt",
        "asset_missing",
        "catalog_corrupt",
        "catalog_missing",
        "record_missing",
    ],
)
def test_resource_closure_preserves_assets_and_rejects_corruption(tmp_path, damage):
    from app.api_server.services.documents.image_catalog import stage_image_catalog

    normalized = tmp_path / "artifacts/a/g1/normalized"
    normalized.mkdir(parents=True)
    (normalized / "a.png").write_bytes(png_bytes())
    built = build_image_catalog(tmp_path, "a", 1, b"![](a.png)", {})
    stage_image_catalog(tmp_path, built.files)
    (tmp_path / "tree.json").write_bytes(b"{}")
    entry = snapshots.SnapshotDocumentEntry(
        document_id="a",
        generation=1,
        index_relpath="tree.json",
        image_catalog_relpath=next(
            path for path in built.files if path.endswith("images.json")
        ),
    )
    records = {
        path: snapshots.file_record(tmp_path, path)
        for path in ["tree.json", *built.files]
    }
    closure = snapshots.document_resource_closure(tmp_path, entry, records)
    assert set(closure) == set(records)
    asset = built.catalog.entries[0].asset_relpath
    if damage == "asset_corrupt":
        (tmp_path / asset).write_bytes(b"corrupt")
    elif damage == "asset_missing":
        (tmp_path / asset).unlink()
    elif damage == "record_missing":
        del records[asset]
    elif damage == "catalog_missing":
        (tmp_path / entry.image_catalog_relpath).unlink()
    else:
        (tmp_path / entry.image_catalog_relpath).write_bytes(b"[]")
        records[entry.image_catalog_relpath] = snapshots.file_record(
            tmp_path, entry.image_catalog_relpath
        )
    with pytest.raises(snapshots.SnapshotError):
        snapshots.document_resource_closure(tmp_path, entry, records)


def test_exact_manifest_budgets_and_path_deduplication(monkeypatch):
    entries = [
        snapshots.SnapshotDocumentEntry(
            document_id=name, generation=1, index_relpath=f"{name}.json"
        )
        for name in ("a", "b")
    ]
    records = [
        snapshots.SnapshotArtifactFile(
            relpath=f"{name}.json", size_bytes=0, sha256="0" * 64
        )
        for name in ("a", "b")
    ]
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_DOCUMENTS", 2)
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_FILES", 2)
    manifest = snapshots.build_manifest("kb", 1, entries, [*records, records[0]])
    assert len(manifest.artifact_files) == 2
    size = len(snapshots.manifest_json_bytes(manifest))
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", size)
    snapshots.build_manifest("kb", 1, entries, records)
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", size - 1)
    with pytest.raises(
        snapshots.SnapshotError, match=f"bytes budget current={size} candidate={size}"
    ):
        snapshots.build_manifest("kb", 1, entries, records, current_manifest=manifest)
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", size)
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_FILES", 1)
    with pytest.raises(snapshots.SnapshotError, match="files budget"):
        snapshots.build_manifest("kb", 1, entries, records)
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_DOCUMENTS", 1)
    with pytest.raises(snapshots.SnapshotError, match="documents budget"):
        snapshots.build_manifest("kb", 1, entries, records)


def test_publish_delete_preserve_images_and_budget_failure_keeps_pointer(
    tmp_path, monkeypatch
):
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb"
    _seed_knowledge_base(repository, workspace)

    def publish(name, content):
        source = f"sources/{name}.md"
        (workspace / "sources").mkdir(exist_ok=True)
        (workspace / source).write_bytes(content)
        repository.create_document(
            {
                "id": name,
                "knowledge_base_id": "kb-1",
                "original_filename": f"{name}.md",
                "file_extension": ".md",
                "mime_type": "text/markdown",
                "size_bytes": len(content),
                "source_relpath": source,
                "source_sha256": hashlib.sha256(content).hexdigest(),
                "parser": "native_markdown",
                "status": "uploaded",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        return snapshots.publish_document_snapshot(
            repository,
            workspace=workspace,
            knowledge_base_id="kb-1",
            document_id=name,
            generation=1,
            content=content,
            source_relpath=source,
            source_mime_type="text/markdown",
            idempotency_key=None,
            fts_enabled=False,
            now=NOW,
            tree_content=json.dumps({"structure": []}).encode(),
        )

    normalized = workspace / "artifacts/a/g1/normalized"
    normalized.mkdir(parents=True)
    (normalized / "a.png").write_bytes(png_bytes())
    publish("a", b"![](a.png)")
    first = snapshots.load_committed_manifest(
        workspace, repository.get_committed_revision("kb-1", 1)
    )
    assets = {
        item.relpath for item in first.artifact_files if "/assets/" in item.relpath
    }
    assert assets
    publish("b", b"text")
    second = snapshots.load_committed_manifest(
        workspace, repository.get_committed_revision("kb-1", 2)
    )
    assert assets <= {item.relpath for item in second.artifact_files}
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_FILES", len(second.artifact_files))
    with pytest.raises(snapshots.SnapshotError, match="files budget current="):
        publish("c", b"other text")
    assert repository.get("knowledge_bases", "kb-1")["content_version"] == 2
    snapshots.publish_document_deletion_snapshot(
        repository,
        workspace=workspace,
        knowledge_base_id="kb-1",
        document_id="b",
        fts_enabled=False,
        now=NOW,
    )
    third = snapshots.load_committed_manifest(
        workspace, repository.get_committed_revision("kb-1", 3)
    )
    assert assets <= {item.relpath for item in third.artifact_files}
    assert len(third.artifact_files) < len(second.artifact_files)
