"""Offline image catalogs from committed local artifacts; no runtime assembly."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.api_server.config import ApiServerSettings, get_settings
from app.api_server.repositories.image_catalog_backfill import (
    ImageCatalogBackfillRepository,
)
from app.api_server.repositories.metadata.publishing import PublishConflictError
from app.api_server.services import workspace_snapshot as snapshots
from app.api_server.services.documents.image_catalog import (
    ImageCatalogBuild,
    build_image_catalog,
    stage_image_catalog,
)
from app.api_server.services.storage_maintenance_lock import StorageMaintenanceLock
from app.api_server.services.workspace_store import workspace_lock
from nianlun.knowledgebase.image_syntax import parse_image_syntax
from nianlun.knowledgebase.workspace_view import workspace_snapshot_registry


@dataclass
class BackfillReport:
    dry_run: bool
    candidates: int = 0
    completed: int = 0
    already_current: int = 0
    missing_artifacts: int = 0
    unsupported: int = 0
    failed: int = 0
    projected_revisions: int = 0
    committed_revisions: int = 0
    documents: list[dict[str, Any]] = field(default_factory=list)
    budgets: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _Candidate:
    entry: snapshots.SnapshotDocumentEntry
    build: ImageCatalogBuild
    source_records: tuple[snapshots.SnapshotArtifactFile, ...]


def _record(raw: bytes, relpath: str) -> snapshots.SnapshotArtifactFile:
    return snapshots.SnapshotArtifactFile(
        relpath=relpath, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()
    )


def _prepare(
    repository: ImageCatalogBackfillRepository,
    workspace: Path,
    entry: snapshots.SnapshotDocumentEntry,
    settings: ApiServerSettings,
) -> _Candidate:
    if entry.content_relpath is None:
        raise FileNotFoundError("committed content artifact is missing")
    artifacts = repository.artifacts(entry.document_id, entry.generation)
    normalized_root = (
        Path("artifacts") / entry.document_id / f"g{entry.generation}" / "normalized"
    )
    registered = {
        str(item["relpath"]): snapshots.SnapshotArtifactFile(
            relpath=str(item["relpath"]),
            size_bytes=int(item["size_bytes"]),
            sha256=str(item["sha256"]),
        )
        for item in artifacts
        if item["kind"] in {"asset", "normalized_markdown"}
    }
    normalized_path = str(normalized_root / "full.md")
    if normalized_path not in registered:
        raise FileNotFoundError("registered normalized Markdown is missing")
    if not snapshots.resolve_confined(workspace, normalized_path).is_file():
        raise FileNotFoundError("normalized Markdown artifact is missing")
    if snapshots.file_record(workspace, normalized_path) != registered[normalized_path]:
        raise snapshots.SnapshotError("normalized Markdown digest changed")
    markdown = snapshots.resolve_confined(workspace, entry.content_relpath).read_bytes()
    used = [registered[normalized_path]]
    # Reject unregistered paths before the builder is allowed to read resources.
    for syntax in parse_image_syntax(markdown.decode("utf-8")).images:
        split = urlsplit(syntax.source)
        if split.scheme or split.netloc:
            continue
        relative = Path(unquote(split.path).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        relpath = str(normalized_root / relative)
        expected = registered.get(relpath)
        if expected is None:
            raise ValueError("image source is not a registered normalized asset")
        try:
            actual = snapshots.file_record(workspace, relpath)
        except snapshots.SnapshotError:
            raise ValueError("registered image asset is missing") from None
        if actual != expected:
            raise snapshots.SnapshotError("registered image asset digest changed")
        used.append(expected)
    tree = json.loads(
        snapshots.resolve_confined(workspace, entry.index_relpath).read_bytes()
    )
    if not isinstance(tree, dict):
        raise ValueError("committed tree is not an object")
    build = build_image_catalog(
        workspace,
        entry.document_id,
        entry.generation,
        markdown,
        tree,
        max_bytes=settings.answer_image_max_image_bytes,
        max_pixels=settings.answer_image_max_image_pixels,
    )
    if build.catalog is None:
        raise ValueError("no supported local image resources")
    return _Candidate(entry, build, tuple(used))


def _candidate_manifest(
    workspace: Path,
    current: snapshots.WorkspaceSnapshotManifest,
    candidates: list[_Candidate],
    virtual_files: dict[str, snapshots.SnapshotArtifactFile] | None = None,
) -> snapshots.WorkspaceSnapshotManifest:
    replacements = {item.entry.document_id: item for item in candidates}
    files: dict[str, snapshots.SnapshotArtifactFile] = {}
    existing = {item.relpath: item for item in current.artifact_files}
    entries = []
    for entry in current.documents:
        candidate = replacements.get(entry.document_id)
        if candidate is None:
            if (
                virtual_files is not None
                and entry.image_catalog_relpath in virtual_files
            ):
                base = entry.model_copy(update={"image_catalog_relpath": None})
                files.update(
                    snapshots.document_resource_closure(workspace, base, existing)
                )
                prefix = str(Path(entry.image_catalog_relpath).parent) + "/"
                files.update(
                    {
                        path: record
                        for path, record in virtual_files.items()
                        if path.startswith(prefix)
                    }
                )
            else:
                files.update(
                    snapshots.document_resource_closure(workspace, entry, existing)
                )
            entries.append(entry)
        else:
            base = entry.model_copy(update={"image_catalog_relpath": None})
            files.update(snapshots.document_resource_closure(workspace, base, existing))
            catalog_path = next(
                path for path in candidate.build.files if path.endswith("/images.json")
            )
            entries.append(
                entry.model_copy(update={"image_catalog_relpath": catalog_path})
            )
            files.update(
                {
                    path: _record(raw, path)
                    for path, raw in candidate.build.files.items()
                }
            )
    return snapshots.WorkspaceSnapshotManifest(
        schema_version=3,
        knowledge_base_id=current.knowledge_base_id,
        content_version=current.content_version + 1,
        documents=entries,
        artifact_files=list(files.values()),
    )


def _budget(
    current: snapshots.WorkspaceSnapshotManifest,
    candidate: snapshots.WorkspaceSnapshotManifest,
) -> dict[str, Any]:
    values = {
        "documents": (
            len(current.documents),
            len(candidate.documents),
            snapshots.MAX_MANIFEST_DOCUMENTS,
        ),
        "files": (
            len(current.artifact_files),
            len(candidate.artifact_files),
            snapshots.MAX_MANIFEST_FILES,
        ),
        "bytes": (
            len(snapshots.manifest_json_bytes(current)),
            len(snapshots.manifest_json_bytes(candidate)),
            snapshots.MAX_MANIFEST_BYTES,
        ),
    }
    return {
        "content_version": candidate.content_version,
        "ok": all(value <= limit for _, value, limit in values.values()),
        "items": {
            key: {"current": old, "candidate": new, "limit": limit}
            for key, (old, new, limit) in values.items()
        },
    }


def _commit(
    repository: ImageCatalogBackfillRepository,
    workspace: Path,
    current: snapshots.WorkspaceSnapshotManifest,
    candidates: list[_Candidate],
) -> int:
    created_bundles: list[Path] = []
    staged: Path | None = None
    final: Path | None = None
    committed = False
    with workspace_lock(workspace):
        kb = repository.knowledge_base(current.knowledge_base_id)
        if int(kb["content_version"]) != current.content_version:
            raise PublishConflictError("content version changed; retry batch")
        revision = repository.revision(
            current.knowledge_base_id, current.content_version
        )
        latest = snapshots.load_committed_manifest(workspace, revision)
        latest_entries = {entry.document_id: entry for entry in latest.documents}
        records = {record.relpath: record for record in current.artifact_files}
        retained = []
        for candidate in candidates:
            entry = candidate.entry
            if (
                repository.document(current.knowledge_base_id, entry.document_id)
                is None
            ):
                continue
            if latest_entries.get(entry.document_id) != entry:
                raise PublishConflictError(
                    "committed generation or artifact pointers changed; retry batch"
                )
            for relpath in (entry.index_relpath, entry.content_relpath):
                if (
                    relpath is not None
                    and snapshots.file_record(workspace, relpath) != records[relpath]
                ):
                    raise PublishConflictError(
                        "committed content/tree digest changed; retry batch"
                    )
            for record in candidate.source_records:
                if snapshots.file_record(workspace, record.relpath) != record:
                    raise PublishConflictError(
                        "normalized source digest changed; retry batch"
                    )
            retained.append(candidate)
        if not retained:
            return 0
        candidate_manifest = _candidate_manifest(workspace, latest, retained)
        if not _budget(latest, candidate_manifest)["ok"]:
            raise snapshots.SnapshotError(
                "manifest budget exceeded; reduce resources or split the knowledge base"
            )
        try:
            rows = []
            for candidate in retained:
                catalog_path = next(
                    path
                    for path in candidate.build.files
                    if path.endswith("/images.json")
                )
                bundle = workspace / Path(catalog_path).parent
                existed = bundle.exists()
                stage_image_catalog(workspace, candidate.build.files)
                if not existed:
                    created_bundles.append(bundle)
                for path, raw in candidate.build.files.items():
                    rows.append(
                        {
                            "document_id": candidate.entry.document_id,
                            "generation": candidate.entry.generation,
                            "kind": "image_catalog"
                            if path.endswith("/images.json")
                            else "image_asset",
                            "relpath": path,
                            "mime_type": "application/json"
                            if path.endswith("/images.json")
                            else "application/octet-stream",
                            "size_bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        }
                    )
            manifest = snapshots.build_manifest(
                current.knowledge_base_id,
                candidate_manifest.content_version,
                candidate_manifest.documents,
                candidate_manifest.artifact_files,
                workspace=workspace,
                current_manifest=latest,
            )
            staged, digest = snapshots.stage_manifest(workspace, manifest)
            target = workspace / snapshots.snapshot_relpath(manifest.content_version)
            # Promotion may rename successfully and then fail while fsyncing.
            # Reserve cleanup ownership before calling it; existing targets
            # belong to an earlier attempt and must remain untouched.
            if not target.exists():
                final = target
            snapshots.promote_staged_snapshot(workspace, staged, manifest)
            repository.commit_batch(
                knowledge_base_id=current.knowledge_base_id,
                expected_version=current.content_version,
                snapshot_relpath=snapshots.snapshot_relpath(manifest.content_version),
                manifest_sha256=digest,
                document_ids=[item.entry.document_id for item in retained],
                artifacts=rows,
                now=datetime.now(timezone.utc).isoformat(),
            )
            committed = True
            return len(retained)
        finally:
            if staged is not None and staged.exists():
                snapshots.discard_staging(staged)
            if not committed:
                if final is not None and final.exists():
                    shutil.rmtree(final)
                for bundle in created_bundles:
                    shutil.rmtree(bundle)


def backfill_document_images(
    settings: ApiServerSettings,
    knowledge_base_id: str,
    *,
    document_id: str | None = None,
    batch_size: int = 100,
    dry_run: bool = False,
) -> BackfillReport:
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= 100
    ):
        raise ValueError("batch_size must be between 1 and 100")
    report = BackfillReport(dry_run=dry_run)
    with StorageMaintenanceLock(settings.data_dir, exclusive=True):
        repository = ImageCatalogBackfillRepository(
            settings.database_path or settings.data_dir / "nianlun.sqlite3",
            dry_run=dry_run,
        )
        try:
            kb = repository.knowledge_base(knowledge_base_id)
            root = settings.workspace_root.expanduser().resolve()
            workspace = (root / str(kb["workspace_relpath"])).resolve()
            workspace.relative_to(root)
            version = int(kb["content_version"])
            initial = snapshots.load_committed_manifest(
                workspace, repository.revision(knowledge_base_id, version)
            )
            ids = [
                entry.document_id
                for entry in initial.documents
                if document_id is None or entry.document_id == document_id
            ]
            if document_id is not None and not ids:
                report.missing_artifacts += 1
                report.documents.append(
                    {
                        "document_id": document_id,
                        "status": "missing",
                        "detail": "document is not committed",
                    }
                )
            position = 0
            projected = initial
            virtual_files: dict[str, snapshots.SnapshotArtifactFile] = {}
            while position < len(ids):
                if dry_run:
                    current = projected
                else:
                    kb = repository.knowledge_base(knowledge_base_id)
                    current = snapshots.load_committed_manifest(
                        workspace,
                        repository.revision(
                            knowledge_base_id, int(kb["content_version"])
                        ),
                    )
                with workspace_snapshot_registry.acquire(
                    workspace / snapshots.snapshot_relpath(current.content_version)
                ):
                    entries = {entry.document_id: entry for entry in current.documents}
                    batch = []
                    while position < len(ids) and len(batch) < batch_size:
                        target_id = ids[position]
                        position += 1
                        entry = entries.get(target_id)
                        if (
                            entry is None
                            or repository.document(knowledge_base_id, target_id) is None
                        ):
                            report.documents.append(
                                {"document_id": target_id, "status": "deleted"}
                            )
                            continue
                        try:
                            candidate = _prepare(repository, workspace, entry, settings)
                            if entry.image_catalog_relpath is not None:
                                snapshots.document_resource_closure(
                                    workspace,
                                    entry,
                                    {
                                        item.relpath: item
                                        for item in current.artifact_files
                                    },
                                )
                                catalog_path = next(
                                    path
                                    for path in candidate.build.files
                                    if path.endswith("/images.json")
                                )
                                same = (
                                    entry.image_catalog_relpath == catalog_path
                                    and all(
                                        snapshots.resolve_confined(
                                            workspace, path
                                        ).is_file()
                                        and snapshots.resolve_confined(
                                            workspace, path
                                        ).read_bytes()
                                        == raw
                                        for path, raw in candidate.build.files.items()
                                    )
                                )
                                if same:
                                    report.already_current += 1
                                    report.documents.append(
                                        {
                                            "document_id": target_id,
                                            "status": "already_current",
                                        }
                                    )
                                    continue
                            report.candidates += 1
                            batch.append(candidate)
                        except (FileNotFoundError, OSError) as exc:
                            report.missing_artifacts += 1
                            report.documents.append(
                                {
                                    "document_id": target_id,
                                    "status": "missing",
                                    "detail": str(exc),
                                }
                            )
                        except ValueError as exc:
                            report.unsupported += 1
                            report.documents.append(
                                {
                                    "document_id": target_id,
                                    "status": "unsupported",
                                    "detail": str(exc),
                                }
                            )
                        except snapshots.SnapshotError as exc:
                            report.failed += 1
                            report.documents.append(
                                {
                                    "document_id": target_id,
                                    "status": "failed",
                                    "detail": str(exc),
                                }
                            )
                    if not batch:
                        continue
                    try:
                        manifest = _candidate_manifest(
                            workspace,
                            current,
                            batch,
                            virtual_files if dry_run else None,
                        )
                        budget = _budget(current, manifest)
                        report.budgets.append(budget)
                        if not budget["ok"]:
                            raise snapshots.SnapshotError(
                                "manifest budget exceeded; reduce resources or split the knowledge base"
                            )
                        report.projected_revisions += 1
                        if dry_run:
                            projected = manifest
                            for item in batch:
                                virtual_files.update(
                                    {
                                        path: _record(raw, path)
                                        for path, raw in item.build.files.items()
                                    }
                                )
                                report.documents.append(
                                    {
                                        "document_id": item.entry.document_id,
                                        "status": "candidate",
                                    }
                                )
                        else:
                            completed = _commit(repository, workspace, current, batch)
                            report.completed += completed
                            report.committed_revisions += int(completed > 0)
                            for item in batch:
                                report.documents.append(
                                    {
                                        "document_id": item.entry.document_id,
                                        "status": "completed"
                                        if repository.document(
                                            knowledge_base_id, item.entry.document_id
                                        )
                                        is not None
                                        else "deleted",
                                    }
                                )
                    except (
                        OSError,
                        sqlite3.Error,
                        snapshots.SnapshotError,
                        PublishConflictError,
                        ValueError,
                    ) as exc:
                        report.failed += len(batch)
                        for item in batch:
                            report.documents.append(
                                {
                                    "document_id": item.entry.document_id,
                                    "status": "failed",
                                    "detail": str(exc),
                                }
                            )
            return report
        finally:
            repository.close()


def main(
    argv: list[str] | None = None, settings: ApiServerSettings | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--document-id")
    parser.add_argument("--batch-size", type=int, choices=range(1, 101), default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = backfill_document_images(
            settings or get_settings(),
            args.knowledge_base_id,
            document_id=args.document_id,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    except (
        OSError,
        sqlite3.Error,
        RuntimeError,
        KeyError,
        ValueError,
        snapshots.SnapshotError,
        PublishConflictError,
    ) as exc:
        print(f"image catalog backfill failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return int(report.failed > 0 or report.missing_artifacts > 0)


if __name__ == "__main__":
    raise SystemExit(main())
