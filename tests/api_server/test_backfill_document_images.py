from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from PIL import Image
import pytest

from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.workspace_snapshot import (
    SnapshotDocumentEntry,
    build_manifest,
    file_record,
    stage_manifest,
    promote_staged_snapshot,
)

NOW = "2026-10-25T00:00:00+00:00"


def seed(tmp_path: Path, count: int = 3):
    settings = ApiServerSettings(
        data_dir=tmp_path,
        workspace_root=tmp_path / "workspaces",
        database_path=tmp_path / "db.sqlite3",
    )
    factory = SQLiteConnectionFactory(settings.database_path)
    initialize_database(factory)
    repository = SQLiteMetadataRepository(factory)
    workspace = settings.workspace_root / "kb"
    workspace.mkdir(parents=True)
    repository.put(
        "knowledge_bases",
        "kb",
        {
            "id": "kb",
            "name": "KB",
            "workspace_relpath": "kb",
            "content_version": 0,
            "document_count": count,
            "fts_status": "ready",
            "vector_status": "ready",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    entries, records = [], []
    image = io.BytesIO()
    Image.new("RGB", (2, 3), "red").save(image, format="PNG")
    raw = image.getvalue()
    for number in range(count):
        doc = f"doc-{number}"
        root = workspace / "artifacts" / doc / "g1"
        (root / "normalized" / "assets").mkdir(parents=True)
        (root / "enriched").mkdir()
        markdown = f"# Title {number}\n\n![pic](assets/pic.png)\n".encode()
        tree = {
            "structure": [{"node_id": "n1", "line_num": 1, "text": markdown.decode()}]
        }
        paths = {
            "normalized/full.md": markdown,
            "normalized/assets/pic.png": raw,
            "enriched/full.md": markdown,
            "enriched/tree.json": json.dumps(tree).encode(),
        }
        for name, data in paths.items():
            (root / name).write_bytes(data)
        prefix = f"artifacts/{doc}/g1/"
        entry = SnapshotDocumentEntry(
            document_id=doc,
            generation=1,
            index_relpath=prefix + "enriched/tree.json",
            content_relpath=prefix + "enriched/full.md",
        )
        entries.append(entry)
        records.extend(
            file_record(workspace, path)
            for path in (entry.index_relpath, entry.content_relpath)
        )
        repository.create_document(
            {
                "id": doc,
                "knowledge_base_id": "kb",
                "original_filename": doc + ".md",
                "file_extension": ".md",
                "mime_type": "text/markdown",
                "size_bytes": len(markdown),
                "source_relpath": prefix + "enriched/full.md",
                "source_sha256": hashlib.sha256(markdown).hexdigest(),
                "parser": "native_markdown",
                "status": "ready",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        for name in ("normalized/full.md", "normalized/assets/pic.png"):
            data = paths[name]
            repository.put_document_artifact(
                {
                    "document_id": doc,
                    "kind": "asset" if name.endswith(".png") else "normalized_markdown",
                    "relpath": prefix + name,
                    "mime_type": "application/octet-stream",
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "pipeline_generation": 1,
                    "created_at": NOW,
                }
            )
    manifest = build_manifest("kb", 0, entries, records)
    staging, digest = stage_manifest(workspace, manifest)
    promote_staged_snapshot(workspace, staging, manifest)
    repository.initialize_kb_revision("kb", 0, "snapshots/r0", digest, NOW)
    factory.dispose()
    return settings, repository, workspace


def test_dry_run_and_batches_are_offline_idempotent(tmp_path: Path) -> None:
    from app.api_server.backfill_document_images import backfill_document_images

    settings, repository, workspace = seed(tmp_path)
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    report = backfill_document_images(settings, "kb", dry_run=True, batch_size=2)
    assert report.candidates == 3
    assert report.projected_revisions == 2
    assert report.committed_revisions == 0
    after = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file() and p.name != ".storage-maintenance.lock"
    }
    assert before == after
    report = backfill_document_images(settings, "kb", batch_size=2)
    assert report.completed == 3
    assert report.committed_revisions == 2
    assert repository.get("knowledge_bases", "kb")["content_version"] == 2
    assert repository.get("knowledge_bases", "kb")["fts_status"] == "pending"
    assert repository.get("knowledge_bases", "kb")["vector_status"] == "pending"
    assert (
        len(list(workspace.glob("artifacts/*/g1/image_catalogs/v1/*/images.json"))) == 3
    )
    again = backfill_document_images(settings, "kb")
    assert again.already_current == 3
    assert again.committed_revisions == 0


def test_missing_artifacts_and_unsupported_do_not_consume_batch(tmp_path: Path) -> None:
    from app.api_server.backfill_document_images import backfill_document_images

    settings, repository, workspace = seed(tmp_path)
    (workspace / "artifacts/doc-0/g1/normalized/full.md").unlink()
    (workspace / "artifacts/doc-1/g1/normalized/assets/pic.png").unlink()
    report = backfill_document_images(settings, "kb", batch_size=1)
    assert report.completed == 1
    assert report.missing_artifacts == 1
    assert report.unsupported == 1
    assert report.committed_revisions == 1


@pytest.mark.parametrize("batch_size", [0, 101, True])
def test_rejects_bad_batch_size_before_storage(tmp_path: Path, batch_size) -> None:
    from app.api_server.backfill_document_images import backfill_document_images

    settings = ApiServerSettings(data_dir=tmp_path / "absent")
    with pytest.raises(ValueError, match="batch_size"):
        backfill_document_images(settings, "kb", batch_size=batch_size)
    assert not settings.data_dir.exists()


def test_default_batch_of_100_creates_one_revision(tmp_path: Path) -> None:
    from app.api_server.backfill_document_images import backfill_document_images

    settings, repository, _ = seed(tmp_path, count=100)
    report = backfill_document_images(settings, "kb")
    assert report.completed == 100
    assert report.committed_revisions == 1
    assert repository.get("knowledge_bases", "kb")["content_version"] == 1


def test_failed_later_batch_preserves_previous_commit_and_cleans_own_files(
    tmp_path: Path, monkeypatch
) -> None:
    from app.api_server.backfill_document_images import backfill_document_images
    from app.api_server.repositories.image_catalog_backfill import (
        ImageCatalogBackfillRepository,
    )
    from app.api_server.repositories.metadata.publishing import PublishConflictError

    settings, repository, workspace = seed(tmp_path)
    original = ImageCatalogBackfillRepository.commit_batch
    calls = 0

    def commit(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PublishConflictError("injected CAS conflict")
        return original(self, **kwargs)

    monkeypatch.setattr(ImageCatalogBackfillRepository, "commit_batch", commit)
    report = backfill_document_images(settings, "kb", batch_size=2)
    assert report.completed == 2
    assert report.failed == 1
    assert report.committed_revisions == 1
    assert repository.get("knowledge_bases", "kb")["content_version"] == 1
    assert (workspace / "snapshots/r1").is_dir()
    assert not (workspace / "snapshots/r2").exists()
    assert not list(
        workspace.glob("artifacts/doc-2/g1/image_catalogs/v1/*/images.json")
    )
    assert not list(workspace.rglob(".staging-*"))


@pytest.mark.parametrize("dry_run", [False, True])
def test_manifest_budget_refuses_whole_batch_without_revision(
    tmp_path: Path, monkeypatch, dry_run: bool
) -> None:
    from app.api_server.backfill_document_images import backfill_document_images
    from app.api_server.services import workspace_snapshot

    settings, repository, workspace = seed(tmp_path)
    monkeypatch.setattr(workspace_snapshot, "MAX_MANIFEST_FILES", 8)
    report = backfill_document_images(settings, "kb", dry_run=dry_run)
    assert report.candidates == 3
    assert report.completed == 0
    assert report.failed == 3
    assert report.projected_revisions == 0
    assert report.budgets[0]["items"]["files"] == {
        "current": 6,
        "candidate": 12,
        "limit": 8,
    }
    assert repository.get("knowledge_bases", "kb")["content_version"] == 0
    assert not list(workspace.glob("artifacts/*/g1/image_catalogs/v1/*/images.json"))


def test_post_rename_fsync_failure_removes_only_owned_uncommitted_closure(
    tmp_path: Path, monkeypatch
) -> None:
    import os
    from app.api_server.backfill_document_images import backfill_document_images
    from app.api_server.services import workspace_snapshot as snapshots

    settings, repository, workspace = seed(tmp_path, count=2)
    original_fsync = snapshots.os.fsync
    snapshots_inode = (workspace / "snapshots").stat().st_ino

    def fail_after_rename(fd):
        if (
            os.fstat(fd).st_ino == snapshots_inode
            and (workspace / "snapshots/r2").exists()
        ):
            raise OSError("injected post-rename directory fsync failure")
        return original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(snapshots.os, "fsync", fail_after_rename)
        report = backfill_document_images(settings, "kb", batch_size=1)
    assert report.completed == 1
    assert report.failed == 1
    assert repository.get("knowledge_bases", "kb")["content_version"] == 1
    committed = repository.get_committed_revision("kb", 1)
    manifest = snapshots.load_committed_manifest(workspace, committed)
    records = {item.relpath: item for item in manifest.artifact_files}
    for entry in manifest.documents:
        snapshots.document_resource_closure(workspace, entry, records)
    assert not (workspace / "snapshots/r2").exists()
    assert not list(workspace.rglob(".staging-*"))
    assert not list(
        workspace.glob("artifacts/doc-1/g1/image_catalogs/v1/*/images.json")
    )

    output = io.BytesIO()
    Image.new("RGB", (2, 3), "blue").save(output, format="PNG")
    raw = output.getvalue()
    path = "artifacts/doc-1/g1/normalized/assets/pic.png"
    (workspace / path).write_bytes(raw)
    repository.put_document_artifact(
        {
            "document_id": "doc-1",
            "kind": "asset",
            "relpath": path,
            "mime_type": "image/png",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "pipeline_generation": 1,
            "created_at": NOW,
        }
    )
    retry = backfill_document_images(settings, "kb", batch_size=1)
    assert retry.completed == 1
    assert retry.already_current == 1
    assert retry.failed == 0
    assert repository.get("knowledge_bases", "kb")["content_version"] == 2


def test_dry_run_refuses_nonempty_wal_without_touching_storage(tmp_path: Path) -> None:
    from app.api_server.backfill_document_images import backfill_document_images

    settings, repository, _ = seed(tmp_path, count=1)
    item = repository.get("knowledge_bases", "kb")
    item["name"] = "Changed"
    repository.put("knowledge_bases", "kb", item)
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert before["db.sqlite3-wal"]
    with pytest.raises(RuntimeError, match="clean SQLite shutdown/checkpoint"):
        backfill_document_images(settings, "kb", dry_run=True)
    after = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file() and p.name != ".storage-maintenance.lock"
    }
    assert before == after


def test_processing_document_uses_committed_generation_without_task_mutation(
    tmp_path: Path,
) -> None:
    from app.api_server.backfill_document_images import backfill_document_images
    from app.api_server.database.models import Document

    settings, repository, workspace = seed(tmp_path, count=1)
    with repository.factory.session_scope(write=True) as session:
        item = session.get(Document, "doc-0")
        item.pipeline_generation = 2
        item.status = "parsing"
    before = repository.get_document("kb", "doc-0")
    report = backfill_document_images(settings, "kb", document_id="doc-0")
    assert report.completed == 1
    assert repository.get_document("kb", "doc-0") == before
    assert list(workspace.glob("artifacts/doc-0/g1/image_catalogs/v1/*/images.json"))
    assert not (workspace / "artifacts/doc-0/g2").exists()


def test_digest_conflict_does_not_publish(tmp_path: Path, monkeypatch) -> None:
    import app.api_server.backfill_document_images as module

    settings, repository, workspace = seed(tmp_path, count=1)
    commit = module._commit

    def conflict(*args):
        (workspace / "artifacts/doc-0/g1/enriched/full.md").write_text("changed")
        return commit(*args)

    monkeypatch.setattr(module, "_commit", conflict)
    report = module.backfill_document_images(settings, "kb")
    assert report.failed == 1
    assert report.completed == 0
    assert repository.get("knowledge_bases", "kb")["content_version"] == 0
    assert not list(
        workspace.glob("artifacts/doc-0/g1/image_catalogs/v1/*/images.json")
    )


def test_dry_run_and_write_report_same_exact_manifest_budgets(tmp_path: Path) -> None:
    from app.api_server.backfill_document_images import backfill_document_images

    settings, _, _ = seed(tmp_path)
    dry = backfill_document_images(settings, "kb", dry_run=True, batch_size=2)
    actual = backfill_document_images(settings, "kb", batch_size=2)
    assert dry.budgets == actual.budgets


def test_configured_image_limits_apply_to_dry_run_and_write(tmp_path: Path) -> None:
    from dataclasses import replace
    from app.api_server.backfill_document_images import backfill_document_images

    settings, repository, _ = seed(tmp_path, count=1)
    settings = replace(settings, answer_image_max_image_pixels=1)
    for dry_run in (True, False):
        report = backfill_document_images(settings, "kb", dry_run=dry_run)
        assert report.unsupported == 1
        assert report.candidates == 0
        assert report.committed_revisions == 0
    assert repository.get("knowledge_bases", "kb")["content_version"] == 0


def test_orphan_matching_bundle_does_not_make_stale_pointer_current(
    tmp_path: Path,
) -> None:
    from app.api_server.backfill_document_images import backfill_document_images
    from app.api_server.services.documents.image_catalog import (
        build_image_catalog,
        stage_image_catalog,
    )

    settings, repository, workspace = seed(tmp_path, count=1)
    assert backfill_document_images(settings, "kb").completed == 1
    output = io.BytesIO()
    Image.new("RGB", (2, 3), "blue").save(output, format="PNG")
    raw = output.getvalue()
    path = "artifacts/doc-0/g1/normalized/assets/pic.png"
    (workspace / path).write_bytes(raw)
    repository.put_document_artifact(
        {
            "document_id": "doc-0",
            "kind": "asset",
            "relpath": path,
            "mime_type": "image/png",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "pipeline_generation": 1,
            "created_at": NOW,
        }
    )
    build = build_image_catalog(
        workspace,
        "doc-0",
        1,
        (workspace / "artifacts/doc-0/g1/enriched/full.md").read_bytes(),
        json.loads((workspace / "artifacts/doc-0/g1/enriched/tree.json").read_bytes()),
    )
    stage_image_catalog(workspace, build.files)
    report = backfill_document_images(settings, "kb")
    assert report.completed == 1
    assert report.already_current == 0
