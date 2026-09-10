"""V2 workspace snapshot: manifest contract, publish primitives, reconciliation.

设计文档 §7.7/§11：知识库可见视图按 content revision 保存为不可变 snapshot，
SQLite `knowledge_base_workspace_revisions` 是当前 revision 的权威来源。
manifest 只引用不可变 artifact（不复制正文），发布遵循单向可见性：
snapshot 目录先落盘并校验，SQLite 事务随后才指向它。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api_server.services.workspace_store import WorkspaceArtifactStore
from nianlun.knowledgebase.workspace_view import workspace_snapshot_registry

if TYPE_CHECKING:
    from app.api_server.repositories import SQLiteMetadataRepository


SNAPSHOT_SCHEMA_VERSION = 2
SNAPSHOT_DIRNAME = "snapshots"
MANIFEST_FILENAME = "manifest.json"

# Manifest 有界性（§7.7）：文档数、文件数、序列化大小与单文件大小均设上限。
MAX_MANIFEST_DOCUMENTS = 10_000
MAX_MANIFEST_FILES = 50_000
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_FILE_BYTES = 2 * 1024 * 1024 * 1024

# 孤儿 snapshot / staging 目录在启动 reconciliation 中的保留期（秒）。
SNAPSHOT_ORPHAN_RETENTION_SECONDS = 24 * 3600.0

_REVISION_DIR = re.compile(r"^r(\d+)$")
_GENERATION_DIR = re.compile(r"^g([1-9]\d*)$")

logger = logging.getLogger(__name__)


class SnapshotError(Exception):
    """Snapshot 契约或发布协议被破坏。"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SnapshotArtifactFile(BaseModel):
    """One immutable file referenced by the manifest."""

    model_config = ConfigDict(extra="forbid")

    relpath: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class SnapshotDocumentEntry(BaseModel):
    """Per-document pointers inside one snapshot revision."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    index_relpath: str = Field(min_length=1)
    content_relpath: str | None = None
    page_map_relpath: str | None = None
    source_relpath: str | None = None
    generation: int = Field(ge=1)
    # Reader-summary fields so KnowledgeBase meta can be built without touching
    # every artifact (§7.7 mandates the first five; these are additive).
    doc_name: str | None = None
    doc_description: str | None = None
    line_count: int | None = Field(default=None, ge=0)
    type: str | None = None


class WorkspaceSnapshotManifest(BaseModel):
    """V2 snapshot manifest (§7.7): one consistent view of a knowledge base."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=2, le=2)
    knowledge_base_id: str = Field(min_length=1)
    content_version: int = Field(ge=0)
    documents: list[SnapshotDocumentEntry]
    artifact_files: list[SnapshotArtifactFile]


def manifest_json_bytes(manifest: WorkspaceSnapshotManifest) -> bytes:
    payload = manifest.model_dump(exclude_none=True)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode(
        "utf-8"
    )


def snapshot_relpath(content_version: int) -> str:
    return f"{SNAPSHOT_DIRNAME}/r{content_version}"


def resolve_confined(workspace: Path, relpath: str) -> Path:
    """Resolve ``relpath`` under ``workspace``; reject traversal/absolute paths."""
    if not relpath or Path(relpath).is_absolute():
        raise SnapshotError(f"snapshot 引用了非法路径: {relpath!r}")
    candidate = (workspace / relpath).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError as exc:
        raise SnapshotError(f"snapshot 引用了 workspace 外的路径: {relpath!r}") from exc
    return candidate


def file_record(workspace: Path, relpath: str) -> SnapshotArtifactFile:
    """Hash one on-disk file into a manifest artifact record."""
    path = resolve_confined(workspace, relpath)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise SnapshotError(f"snapshot 引用的文件不可读: {relpath}") from exc
    return SnapshotArtifactFile(
        relpath=relpath,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def build_manifest(
    knowledge_base_id: str,
    content_version: int,
    documents: Sequence[SnapshotDocumentEntry],
    artifact_files: Iterable[SnapshotArtifactFile],
) -> WorkspaceSnapshotManifest:
    if len(documents) > MAX_MANIFEST_DOCUMENTS:
        raise SnapshotError(f"manifest 文档数超限: {len(documents)}")
    files: dict[str, SnapshotArtifactFile] = {}
    for item in artifact_files:
        files.setdefault(item.relpath, item)
    if len(files) > MAX_MANIFEST_FILES:
        raise SnapshotError(f"manifest 文件数超限: {len(files)}")
    manifest = WorkspaceSnapshotManifest(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        knowledge_base_id=knowledge_base_id,
        content_version=content_version,
        documents=list(documents),
        artifact_files=list(files.values()),
    )
    seen_documents: set[str] = set()
    for entry in manifest.documents:
        if entry.document_id in seen_documents:
            raise SnapshotError(f"manifest 文档重复: {entry.document_id}")
        seen_documents.add(entry.document_id)
        _require_confined_relpath(entry.index_relpath)
        for optional in (
            entry.content_relpath,
            entry.page_map_relpath,
            entry.source_relpath,
        ):
            if optional is not None:
                _require_confined_relpath(optional)
    for item in manifest.artifact_files:
        _require_confined_relpath(item.relpath)
        if item.size_bytes > MAX_ARTIFACT_FILE_BYTES:
            raise SnapshotError(f"manifest 引用文件过大: {item.relpath}")
    missing = {
        relpath
        for entry in manifest.documents
        for relpath in (
            entry.index_relpath,
            entry.content_relpath,
            entry.page_map_relpath,
            entry.source_relpath,
        )
        if relpath is not None and relpath not in files
    }
    if missing:
        raise SnapshotError(f"manifest 缺少 artifact file 记录: {sorted(missing)!r}")
    payload = manifest_json_bytes(manifest)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise SnapshotError(f"manifest 序列化过大: {len(payload)} 字节")
    return manifest


def _require_confined_relpath(relpath: str) -> None:
    # Shape-only validation; the workspace root is applied when reading/publishing.
    if not relpath or Path(relpath).is_absolute() or ".." in Path(relpath).parts:
        raise SnapshotError(f"snapshot 引用了非法路径: {relpath!r}")


def stage_manifest(
    workspace: Path, manifest: WorkspaceSnapshotManifest
) -> tuple[Path, str]:
    """Write the manifest into a lease-token staging directory (§11.2 step 5)."""
    snapshots = workspace / SNAPSHOT_DIRNAME
    snapshots.mkdir(parents=True, exist_ok=True)
    staging = snapshots / f".staging-{uuid.uuid4().hex}"
    payload = manifest_json_bytes(manifest)
    try:
        staging.mkdir()
        WorkspaceArtifactStore.atomic_write(staging / MANIFEST_FILENAME, payload)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging, hashlib.sha256(payload).hexdigest()


def discard_staging(staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)


def promote_staged_snapshot(
    workspace: Path, staging: Path, manifest: WorkspaceSnapshotManifest
) -> Path:
    """Atomically rename the staging dir to ``snapshots/r<N>`` (§11.2 step 6)."""
    final = workspace / SNAPSHOT_DIRNAME / f"r{manifest.content_version}"
    if final.exists():
        try:
            existing = _parse_manifest_bytes((final / MANIFEST_FILENAME).read_bytes())
        except (OSError, SnapshotError) as exc:
            raise SnapshotError(f"目标 snapshot 已存在但不可读: {final}") from exc
        if manifest_json_bytes(existing) == manifest_json_bytes(manifest):
            discard_staging(staging)
            return final
        raise SnapshotError(f"目标 snapshot 已存在且内容不一致: {final}")
    try:
        staging.replace(final)
    except OSError as exc:
        discard_staging(staging)
        raise SnapshotError(f"snapshot 目录发布失败: {final}") from exc
    directory_fd = os.open(final.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return final


def quarantine_uncommitted_snapshot(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    knowledge_base_id: str,
    content_version: int,
) -> None:
    """Free a version slot when SQLite did not commit its promoted snapshot."""
    try:
        committed = repository.get_committed_revision(knowledge_base_id, content_version)
        if committed is not None:
            return
        final = workspace / SNAPSHOT_DIRNAME / f"r{content_version}"
        if not final.is_dir():
            return
        orphan = final.with_name(
            f".orphan-r{content_version}-{uuid.uuid4().hex[:8]}"
        )
        final.replace(orphan)
        logger.warning(
            "snapshot.uncommitted_quarantined knowledge_base_id=%s content_version=%s",
            knowledge_base_id,
            content_version,
        )
    except Exception:
        # Preserve the original persistence failure; startup GC can later clean
        # a directory that could not be quarantined here.
        logger.exception(
            "snapshot.uncommitted_quarantine_failed knowledge_base_id=%s content_version=%s",
            knowledge_base_id,
            content_version,
        )


def quarantine_untracked_snapshots(
    workspace: Path, valid_content_versions: Iterable[int]
) -> list[str]:
    """Immediately free revision slots absent from SQLite's revision chain."""
    snapshots = workspace / SNAPSHOT_DIRNAME
    if not snapshots.is_dir():
        return []
    valid = {int(version) for version in valid_content_versions}
    quarantined: list[str] = []
    for child in snapshots.iterdir():
        match = _REVISION_DIR.fullmatch(child.name)
        if match is None or int(match.group(1)) in valid or not child.is_dir():
            continue
        orphan = child.with_name(
            f".orphan-{child.name}-{uuid.uuid4().hex[:8]}"
        )
        try:
            child.replace(orphan)
        except OSError:
            logger.warning("snapshot.untracked_quarantine_failed path=%s", child)
            continue
        quarantined.append(child.name)
        logger.warning("snapshot.untracked_quarantined path=%s", child)
    return quarantined


def _parse_manifest_bytes(payload: bytes) -> WorkspaceSnapshotManifest:
    try:
        manifest = WorkspaceSnapshotManifest.model_validate_json(payload)
    except ValidationError as exc:
        raise SnapshotError(f"manifest 格式无效: {exc}") from exc
    if manifest.schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotError(f"manifest schema 版本不支持: {manifest.schema_version}")
    return manifest


def read_manifest_file(path: Path) -> WorkspaceSnapshotManifest:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SnapshotError(f"manifest 不可读: {path}") from exc
    return _parse_manifest_bytes(payload)


def verify_manifest(
    workspace: Path,
    revision_snapshot_relpath: str,
    manifest_sha256: str,
    knowledge_base_id: str,
    content_version: int,
) -> WorkspaceSnapshotManifest:
    """Open + verify one committed snapshot (hash, kb, revision)."""
    manifest_path = resolve_confined(
        workspace, f"{revision_snapshot_relpath}/{MANIFEST_FILENAME}"
    )
    try:
        payload = manifest_path.read_bytes()
    except OSError as exc:
        raise SnapshotError(f"committed snapshot 缺失: {manifest_path}") from exc
    if hashlib.sha256(payload).hexdigest() != manifest_sha256:
        raise SnapshotError(f"manifest hash 不匹配: {manifest_path}")
    manifest = _parse_manifest_bytes(payload)
    if manifest.knowledge_base_id != knowledge_base_id:
        raise SnapshotError("manifest 的 knowledge_base_id 不匹配")
    if manifest.content_version != content_version:
        raise SnapshotError("manifest 的 content_version 不匹配")
    return manifest


def load_committed_manifest(
    workspace: Path, revision: Mapping[str, Any]
) -> WorkspaceSnapshotManifest:
    return verify_manifest(
        workspace,
        str(revision["snapshot_relpath"]),
        str(revision["manifest_sha256"]),
        str(revision["knowledge_base_id"]),
        int(revision["content_version"]),
    )


def generation_stage_dir(document_id: str, generation: int, stage: str) -> Path:
    if stage not in {"normalized", "enriched"}:
        raise SnapshotError(f"不支持的 generation artifact 阶段: {stage}")
    return Path("artifacts") / document_id / f"g{generation}" / stage


def generation_enriched_dir(document_id: str, generation: int) -> Path:
    return generation_stage_dir(document_id, generation, "enriched")


def stage_generation_artifacts(
    workspace: Path,
    document_id: str,
    generation: int,
    files: Mapping[str, bytes],
) -> dict[str, str]:
    return stage_generation_stage_artifacts(
        workspace, document_id, generation, "enriched", files
    )


def stage_generation_stage_artifacts(
    workspace: Path,
    document_id: str,
    generation: int,
    stage: str,
    files: Mapping[str, bytes],
) -> dict[str, str]:
    """Publish immutable generation artifacts via temp-dir rename (§11.2 step 3).

    目标已存在时逐文件校验 hash：一致视为幂等成功，不一致拒绝覆盖。
    """
    if not files:
        raise SnapshotError("generation 产物为空")
    base = generation_stage_dir(document_id, generation, stage)
    target = workspace / base
    if target.exists():
        staged: dict[str, str] = {}
        for name, content in files.items():
            _require_confined_relpath(name)
            path = target / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != (
                hashlib.sha256(content).hexdigest()
            ):
                raise SnapshotError(
                    f"generation 产物已存在且内容不一致: {target / name}"
                )
            staged[name] = str(base / name)
        return staged
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".staging-{target.name}-{uuid.uuid4().hex}"
    try:
        tmp.mkdir()
        for name, content in files.items():
            _require_confined_relpath(name)
            path = tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            WorkspaceArtifactStore.atomic_write(path, content)
        tmp.replace(target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {name: str(base / name) for name in files}


def bootstrap_manifest_state(
    workspace: Path, knowledge_base_id: str
) -> tuple[dict[str, SnapshotDocumentEntry], dict[str, SnapshotArtifactFile]]:
    """Bridge legacy V1 workspaces (root ``_meta.json``) into manifest entries.

    根目录 ``<doc_id>.json`` 在存量库升级时充当一代 index artifact；文档下次
    发布时会被 ``artifacts/<doc>/g1/enriched/tree.json`` 替换为不可变副本。
    """
    meta_path = workspace / "_meta.json"
    entries: dict[str, SnapshotDocumentEntry] = {}
    files: dict[str, SnapshotArtifactFile] = {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    for document_id, info in meta.items():
        if not isinstance(document_id, str) or not isinstance(info, dict):
            continue
        root_index = workspace / f"{document_id}.json"
        if not root_index.is_file():
            continue
        try:
            tree_content = root_index.read_bytes()
            json.loads(tree_content)
        except (OSError, json.JSONDecodeError):
            continue
        content_relpath = info.get("path")
        content_path = (
            Path(content_relpath)
            if isinstance(content_relpath, str) and Path(content_relpath).is_absolute()
            else workspace / str(content_relpath or "")
        )
        immutable_files = {"tree.json": tree_content}
        if content_path.is_file():
            immutable_files["full.md"] = content_path.read_bytes()
        immutable = stage_generation_artifacts(
            workspace, document_id, 1, immutable_files
        )
        index_relpath = immutable["tree.json"]
        immutable_content_relpath = immutable.get("full.md")
        line_count = info.get("line_count")
        entries[document_id] = SnapshotDocumentEntry(
            document_id=document_id,
            index_relpath=index_relpath,
            content_relpath=immutable_content_relpath,
            generation=1,
            doc_name=(
                str(info.get("doc_name")) if info.get("doc_name") is not None else None
            ),
            doc_description=(
                str(info.get("doc_description"))
                if info.get("doc_description") is not None
                else None
            ),
            line_count=int(line_count) if isinstance(line_count, int) else None,
            type=str(info.get("type")) if info.get("type") is not None else None,
        )
        files[index_relpath] = file_record(workspace, index_relpath)
        if immutable_content_relpath is not None:
            files[immutable_content_relpath] = file_record(
                workspace, immutable_content_relpath
            )
    return entries, files


def publish_document_snapshot(
    repository: SQLiteMetadataRepository,
    *,
    workspace: Path,
    knowledge_base_id: str,
    document_id: str,
    generation: int,
    content: bytes,
    source_relpath: str,
    source_mime_type: str,
    idempotency_key: str | None,
    fts_enabled: bool,
    now: str,
    page_map_relpath: str | None = None,
    document_values: dict[str, Any] | None = None,
    tree_content: bytes | None = None,
    enrichment_completion: dict[str, Any] | None = None,
) -> int:
    """Publish one document artifact bundle and atomically advance SQLite."""
    knowledge_base = repository.get("knowledge_bases", knowledge_base_id)
    if knowledge_base is None:
        raise KeyError(knowledge_base_id)
    current_version = int(knowledge_base.get("content_version", 0))
    current_revision = repository.get_committed_revision(
        knowledge_base_id, current_version
    )
    if current_revision is None:
        _bootstrap_revision(
            repository, workspace, knowledge_base_id, current_version, now
        )
        current_revision = repository.get_committed_revision(
            knowledge_base_id, current_version
        )
    if current_revision is None:
        raise SnapshotError("知识库当前 revision 不存在或未 committed")
    current_manifest = load_committed_manifest(workspace, current_revision)

    try:
        if tree_content is None:
            tree_content = (workspace / f"{document_id}.json").read_bytes()
        tree_payload = json.loads(tree_content)
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"文档 tree artifact 不可读: {document_id}") from exc
    if not isinstance(tree_payload, dict):
        raise SnapshotError(f"文档 tree artifact 格式无效: {document_id}")
    immutable = stage_generation_artifacts(
        workspace,
        document_id,
        generation,
        {"tree.json": tree_content, "full.md": content},
    )
    tree_relpath = immutable["tree.json"]
    content_relpath = immutable["full.md"]
    entry = SnapshotDocumentEntry(
        document_id=document_id,
        index_relpath=tree_relpath,
        content_relpath=content_relpath,
        page_map_relpath=page_map_relpath,
        source_relpath=source_relpath,
        generation=generation,
        doc_name=str(tree_payload.get("doc_name") or "unknown"),
        doc_description=str(tree_payload.get("doc_description") or ""),
        line_count=int(tree_payload.get("line_count") or 0),
        type=str(tree_payload.get("type") or "md"),
    )

    entries = {
        item.document_id: item
        for item in current_manifest.documents
        if item.document_id != document_id
    }
    entries[document_id] = entry
    referenced = {
        relpath
        for item in entries.values()
        for relpath in (
            item.index_relpath,
            item.content_relpath,
            item.page_map_relpath,
            item.source_relpath,
        )
        if relpath is not None
    }
    old_files = {
        item.relpath: item
        for item in current_manifest.artifact_files
        if item.relpath in referenced
    }
    old_files[tree_relpath] = file_record(workspace, tree_relpath)
    old_files[content_relpath] = file_record(workspace, content_relpath)
    old_files[source_relpath] = file_record(workspace, source_relpath)
    if page_map_relpath is not None:
        old_files[page_map_relpath] = file_record(workspace, page_map_relpath)
    next_version = current_version + 1
    manifest = build_manifest(
        knowledge_base_id,
        next_version,
        list(entries.values()),
        old_files.values(),
    )
    staging, manifest_sha256 = stage_manifest(workspace, manifest)
    try:
        promote_staged_snapshot(workspace, staging, manifest)
    except Exception:
        discard_staging(staging)
        raise

    vector_enabled = knowledge_base.get("vector_status") != "disabled"
    artifact_rows = [
        {
            "kind": "original",
            "relpath": source_relpath,
            "mime_type": source_mime_type,
            "size_bytes": resolve_confined(workspace, source_relpath).stat().st_size,
            "sha256": file_record(workspace, source_relpath).sha256,
        },
        {
            "kind": "tree",
            "relpath": tree_relpath,
            "mime_type": "application/json",
            "size_bytes": len(tree_content),
            "sha256": hashlib.sha256(tree_content).hexdigest(),
        },
        {
            "kind": "enriched_markdown",
            "relpath": content_relpath,
            "mime_type": "text/markdown",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    ]
    try:
        revision = repository.publish_document_revision(
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_count=len(entries),
            expected_content_version=current_version,
            next_content_version=next_version,
            snapshot_relpath=snapshot_relpath(next_version),
            manifest_sha256=manifest_sha256,
            artifacts=artifact_rows,
            parsed_markdown_relpath=content_relpath,
            generation=generation,
            idempotency_key=idempotency_key,
            fts_collection=str(knowledge_base.get("fts_collection") or ""),
            index_statuses=(fts_enabled, vector_enabled),
            now=now,
            document_values=document_values,
            enrichment_completion=enrichment_completion,
        )
    except Exception:
        quarantine_uncommitted_snapshot(
            repository,
            workspace,
            knowledge_base_id,
            next_version,
        )
        raise
    repair_v1_projection(workspace, manifest)
    return revision


def publish_document_deletion_snapshot(
    repository: SQLiteMetadataRepository,
    *,
    workspace: Path,
    knowledge_base_id: str,
    document_id: str,
    fts_enabled: bool,
    now: str,
) -> int:
    """Publish a revision without ``document_id`` and retain its tombstone."""
    knowledge_base = repository.get("knowledge_bases", knowledge_base_id)
    if knowledge_base is None:
        raise KeyError(knowledge_base_id)
    current_version = int(knowledge_base.get("content_version", 0))
    current_revision = repository.get_committed_revision(
        knowledge_base_id, current_version
    )
    if current_revision is None:
        _bootstrap_revision(
            repository, workspace, knowledge_base_id, current_version, now
        )
        current_revision = repository.get_committed_revision(
            knowledge_base_id, current_version
        )
    if current_revision is None:
        raise SnapshotError("知识库当前 revision 不存在或未 committed")
    current_manifest = load_committed_manifest(workspace, current_revision)
    entries = [
        item for item in current_manifest.documents if item.document_id != document_id
    ]
    referenced = {
        relpath
        for item in entries
        for relpath in (
            item.index_relpath,
            item.content_relpath,
            item.page_map_relpath,
            item.source_relpath,
        )
        if relpath is not None
    }
    files = [
        item for item in current_manifest.artifact_files if item.relpath in referenced
    ]
    next_version = current_version + 1
    manifest = build_manifest(knowledge_base_id, next_version, entries, files)
    staging, manifest_sha256 = stage_manifest(workspace, manifest)
    try:
        promote_staged_snapshot(workspace, staging, manifest)
    except Exception:
        discard_staging(staging)
        raise
    vector_enabled = knowledge_base.get("vector_status") != "disabled"
    try:
        revision = repository.publish_document_deletion_revision(
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            expected_content_version=current_version,
            next_content_version=next_version,
            snapshot_relpath=snapshot_relpath(next_version),
            manifest_sha256=manifest_sha256,
            document_count=len(entries),
            index_statuses=(fts_enabled, vector_enabled),
            now=now,
        )
    except Exception:
        quarantine_uncommitted_snapshot(
            repository,
            workspace,
            knowledge_base_id,
            next_version,
        )
        raise
    repair_v1_projection(workspace, manifest)
    return revision


def cleanup_snapshot_orphans(
    workspace: Path,
    valid_content_versions: Iterable[int],
    retention_seconds: float = SNAPSHOT_ORPHAN_RETENTION_SECONDS,
) -> list[str]:
    """Remove expired ``.staging-*`` and untracked ``r<N>`` snapshot dirs (§11.2)."""
    snapshots = workspace / SNAPSHOT_DIRNAME
    if not snapshots.is_dir():
        return []
    valid = {int(version) for version in valid_content_versions}
    threshold = time.time() - max(retention_seconds, 0.0)
    removed: list[str] = []
    for child in snapshots.iterdir():
        try:
            expired = child.stat().st_mtime < threshold
        except OSError:
            continue
        if not expired:
            continue
        name = child.name
        if name.startswith(".staging-") or name.startswith(".orphan-"):
            target = child
        else:
            match = _REVISION_DIR.match(name)
            if match is None or int(match.group(1)) in valid:
                continue
            target = child
        shutil.rmtree(target, ignore_errors=True)
        removed.append(name)
    return removed


def cleanup_superseded_snapshots(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    knowledge_base_id: str,
    revisions: Iterable[dict[str, Any]],
    retention_seconds: float = SNAPSHOT_ORPHAN_RETENTION_SECONDS,
) -> list[int]:
    """Delete expired superseded revisions only after all readers release them."""
    threshold = time.time() - max(retention_seconds, 0.0)
    removed: list[int] = []
    for revision in revisions:
        if revision.get("state") != "superseded":
            continue
        content_version = int(revision["content_version"])
        target = resolve_confined(workspace, str(revision["snapshot_relpath"]))
        try:
            if not target.is_dir() or target.stat().st_mtime >= threshold:
                continue
        except OSError:
            continue
        deleted = workspace_snapshot_registry.run_if_unreferenced(
            target, lambda: shutil.rmtree(target)
        )
        if not deleted:
            continue
        repository.delete_superseded_revision(knowledge_base_id, content_version)
        removed.append(content_version)
    return removed


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_symlink() or not item.is_file():
                continue
            total += item.stat().st_size
        except OSError:
            continue
    return total


def _remove_managed_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def reconcile_staging_allocations(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    knowledge_base_id: str,
    *,
    now: str,
    retention_seconds: float,
) -> list[str]:
    """Rebuild staging accounting before workers claim persisted tasks."""
    staging_root = workspace / "staging"
    active_tasks = repository.list_active_task_generation_refs(knowledge_base_id)
    active_allocations = repository.list_active_staging_generation_refs(
        knowledge_base_id
    )
    documents = {
        str(item["id"]): item
        for item in repository.list_documents_including_deleted(knowledge_base_id)
    }
    threshold = time.time() - max(retention_seconds, 0.0)
    seen: set[tuple[str, int]] = set()
    removed: list[str] = []
    if staging_root.is_dir():
        for document_dir in staging_root.iterdir():
            if document_dir.is_symlink() or not document_dir.is_dir():
                try:
                    expired = document_dir.stat().st_mtime < threshold
                except OSError:
                    continue
                if expired:
                    _remove_managed_path(document_dir)
                    removed.append(str(document_dir.relative_to(workspace)))
                continue
            document_id = document_dir.name
            for generation_dir in document_dir.iterdir():
                match = _GENERATION_DIR.fullmatch(generation_dir.name)
                try:
                    expired = generation_dir.stat().st_mtime < threshold
                except OSError:
                    continue
                if (
                    match is None
                    or generation_dir.is_symlink()
                    or not generation_dir.is_dir()
                ):
                    if expired:
                        _remove_managed_path(generation_dir)
                        removed.append(str(generation_dir.relative_to(workspace)))
                    continue
                generation = int(match.group(1))
                ref = (document_id, generation)
                seen.add(ref)
                document = documents.get(document_id)
                is_current_unpublished = bool(
                    document is not None
                    and int(document["pipeline_generation"]) == generation
                    and document["status"] not in {"indexing", "ready", "deleted"}
                )
                if document is not None and (
                    ref in active_tasks or is_current_unpublished
                ):
                    repository.reconcile_generation_staging_bytes(
                        document_id,
                        generation,
                        _directory_size(generation_dir),
                        now,
                    )
                    continue
                if document is not None:
                    repository.release_generation_staging_allocations(
                        document_id, generation, now
                    )
                if expired and ref not in active_tasks:
                    _remove_managed_path(generation_dir)
                    removed.append(str(generation_dir.relative_to(workspace)))
                elif document is None:
                    logger.warning(
                        "staging.reconcile_unowned knowledge_base_id=%s path=%s",
                        knowledge_base_id,
                        generation_dir,
                    )
    for document_id, generation in active_allocations - seen:
        repository.release_generation_staging_allocations(document_id, generation, now)
    return removed


def cleanup_unreferenced_generation_artifacts(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    knowledge_base_id: str,
    *,
    retention_seconds: float,
) -> list[str]:
    """Remove expired generation dirs only when no durable owner references them."""
    artifacts_root = workspace / "artifacts"
    if not artifacts_root.is_dir():
        return []
    protected = repository.list_active_task_generation_refs(knowledge_base_id)
    protected.update(repository.list_active_staging_generation_refs(knowledge_base_id))
    for document in repository.list_documents_including_deleted(knowledge_base_id):
        if document["status"] != "deleted":
            protected.add((str(document["id"]), int(document["pipeline_generation"])))
    for revision in repository.list_revisions(knowledge_base_id):
        try:
            manifest = load_committed_manifest(workspace, revision)
        except SnapshotError:
            logger.warning(
                "artifact.gc_revision_invalid knowledge_base_id=%s revision=%s",
                knowledge_base_id,
                revision.get("content_version"),
                exc_info=True,
            )
            return []
        protected.update(
            (entry.document_id, entry.generation) for entry in manifest.documents
        )

    threshold = time.time() - max(retention_seconds, 0.0)
    removed: list[str] = []
    for document_dir in artifacts_root.iterdir():
        if document_dir.is_symlink() or not document_dir.is_dir():
            continue
        for generation_dir in document_dir.iterdir():
            match = _GENERATION_DIR.fullmatch(generation_dir.name)
            if (
                match is None
                or generation_dir.is_symlink()
                or not generation_dir.is_dir()
            ):
                continue
            ref = (document_dir.name, int(match.group(1)))
            try:
                expired = generation_dir.stat().st_mtime < threshold
            except OSError:
                continue
            if ref in protected or not expired:
                continue
            shutil.rmtree(generation_dir, ignore_errors=True)
            removed.append(str(generation_dir.relative_to(workspace)))
        try:
            document_dir.rmdir()
        except OSError:
            pass
    return removed


def repair_v1_projection(workspace: Path, manifest: WorkspaceSnapshotManifest) -> None:
    """Bring the root V1 projection back in line with the committed snapshot."""
    expected_meta: dict[str, dict[str, Any]] = {}
    for entry in manifest.documents:
        root_index = workspace / f"{entry.document_id}.json"
        source = resolve_confined(workspace, entry.index_relpath)
        if source.is_file():
            expected = source.read_bytes()
            try:
                current_index = root_index.read_bytes()
            except OSError:
                current_index = b""
            if current_index != expected:
                WorkspaceArtifactStore.atomic_write(root_index, expected)
        expected_meta[entry.document_id] = {
            "type": entry.type or "md",
            "doc_name": entry.doc_name or "unknown",
            "doc_description": entry.doc_description or "",
            "path": entry.content_relpath or "",
            "line_count": entry.line_count or 0,
        }
    meta_path = workspace / "_meta.json"
    try:
        current = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    if current != expected_meta:
        WorkspaceArtifactStore.atomic_write(
            meta_path,
            json.dumps(
                expected_meta, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8"),
        )
    for stale_document_id in set(current) - set(expected_meta):
        (workspace / f"{stale_document_id}.json").unlink(missing_ok=True)


def reconcile_knowledge_base_workspace(
    repository: SQLiteMetadataRepository,
    knowledge_base_id: str,
    workspace: Path,
    *,
    now: str,
    orphan_retention_seconds: float = SNAPSHOT_ORPHAN_RETENTION_SECONDS,
) -> str:
    """Startup reconciliation for one knowledge base (§11.2 尾段).

    Returns ``"ok"`` / ``"bootstrapped"`` / ``"error"``.
    """
    current = int(
        repository.get("knowledge_bases", knowledge_base_id)["content_version"]
    )
    revision = repository.get_committed_revision(knowledge_base_id, current)
    if revision is not None:
        try:
            manifest = load_committed_manifest(workspace, revision)
        except SnapshotError as exc:
            logger.error(
                "snapshot.reconcile_invalid knowledge_base_id=%s content_version=%s error=%s",
                knowledge_base_id,
                current,
                exc,
            )
            repository.mark_knowledge_base_unavailable(
                knowledge_base_id, f"workspace snapshot 校验失败: {exc}", now
            )
            return "error"
        repair_v1_projection(workspace, manifest)
        revisions = repository.list_revisions(knowledge_base_id)
        quarantine_untracked_snapshots(
            workspace, [item["content_version"] for item in revisions]
        )
        cleanup_snapshot_orphans(
            workspace,
            [item["content_version"] for item in revisions],
            orphan_retention_seconds,
        )
        cleanup_superseded_snapshots(
            repository,
            workspace,
            knowledge_base_id,
            revisions,
            orphan_retention_seconds,
        )
        return "ok"
    existing = repository.get_revision(knowledge_base_id, current)
    if existing is not None:
        repository.mark_knowledge_base_unavailable(
            knowledge_base_id,
            f"workspace revision {current} 不是 committed 状态",
            now,
        )
        return "error"
    # No revision row for the current pointer: bootstrap one from the V1 root so
    # the DB pointer and snapshot chain line up (covers r0 and legacy upgrades).
    _bootstrap_revision(repository, workspace, knowledge_base_id, current, now)
    return "bootstrapped"


def _bootstrap_revision(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    knowledge_base_id: str,
    content_version: int,
    now: str,
) -> None:
    entries, files = bootstrap_manifest_state(workspace, knowledge_base_id)
    manifest = build_manifest(
        knowledge_base_id, content_version, list(entries.values()), files.values()
    )
    staging, manifest_sha256 = stage_manifest(workspace, manifest)
    try:
        try:
            promote_staged_snapshot(workspace, staging, manifest)
        except SnapshotError:
            # An untracked directory occupies the target slot; quarantine and retry.
            stale = workspace / SNAPSHOT_DIRNAME / f"r{content_version}"
            stale.rename(
                workspace
                / SNAPSHOT_DIRNAME
                / f".orphan-r{content_version}-{uuid.uuid4().hex[:8]}"
            )
            staging, manifest_sha256 = stage_manifest(workspace, manifest)
            promote_staged_snapshot(workspace, staging, manifest)
    except Exception:
        discard_staging(staging)
        raise
    if not repository.initialize_kb_revision(
        knowledge_base_id,
        content_version,
        snapshot_relpath(content_version),
        manifest_sha256,
        now,
    ):
        logger.error(
            "snapshot.bootstrap_cas_failed knowledge_base_id=%s content_version=%s",
            knowledge_base_id,
            content_version,
        )


def reconcile_workspace_revisions(
    repository: SQLiteMetadataRepository,
    knowledge_base_service: Any,
    *,
    orphan_retention_seconds: float = SNAPSHOT_ORPHAN_RETENTION_SECONDS,
) -> None:
    """Reconcile every knowledge base before background workers start."""
    now = _utc_now().isoformat()
    for item in repository.list("knowledge_bases"):
        knowledge_base_id = str(item["id"])
        try:
            workspace = knowledge_base_service.workspace_path_for(item)
            outcome = reconcile_knowledge_base_workspace(
                repository,
                knowledge_base_id,
                workspace,
                now=now,
                orphan_retention_seconds=orphan_retention_seconds,
            )
            if outcome != "ok":
                logger.info(
                    "snapshot.reconcile knowledge_base_id=%s outcome=%s",
                    knowledge_base_id,
                    outcome,
                )
        except Exception:
            logger.exception(
                "snapshot.reconcile_failed knowledge_base_id=%s", knowledge_base_id
            )


def reconcile_workspace_storage(
    repository: SQLiteMetadataRepository,
    knowledge_base_service: Any,
    *,
    staging_retention_seconds: float,
    artifact_retention_seconds: float,
) -> None:
    """Reconcile staging quota and generation GC before pipeline workers start."""
    now = _utc_now().isoformat()
    for item in repository.list("knowledge_bases"):
        knowledge_base_id = str(item["id"])
        try:
            workspace = knowledge_base_service.workspace_path_for(item)
            reconcile_staging_allocations(
                repository,
                workspace,
                knowledge_base_id,
                now=now,
                retention_seconds=staging_retention_seconds,
            )
            cleanup_unreferenced_generation_artifacts(
                repository,
                workspace,
                knowledge_base_id,
                retention_seconds=artifact_retention_seconds,
            )
        except Exception:
            logger.exception(
                "workspace.storage_reconcile_failed knowledge_base_id=%s",
                knowledge_base_id,
            )


__all__ = [
    "MAX_ARTIFACT_FILE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_MANIFEST_DOCUMENTS",
    "MAX_MANIFEST_FILES",
    "SNAPSHOT_ORPHAN_RETENTION_SECONDS",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotArtifactFile",
    "SnapshotDocumentEntry",
    "SnapshotError",
    "WorkspaceSnapshotManifest",
    "bootstrap_manifest_state",
    "build_manifest",
    "cleanup_snapshot_orphans",
    "cleanup_superseded_snapshots",
    "cleanup_unreferenced_generation_artifacts",
    "discard_staging",
    "file_record",
    "generation_enriched_dir",
    "load_committed_manifest",
    "manifest_json_bytes",
    "promote_staged_snapshot",
    "publish_document_deletion_snapshot",
    "publish_document_snapshot",
    "quarantine_uncommitted_snapshot",
    "quarantine_untracked_snapshots",
    "read_manifest_file",
    "reconcile_knowledge_base_workspace",
    "reconcile_staging_allocations",
    "reconcile_workspace_storage",
    "reconcile_workspace_revisions",
    "repair_v1_projection",
    "resolve_confined",
    "snapshot_relpath",
    "stage_generation_artifacts",
    "stage_generation_stage_artifacts",
    "stage_manifest",
    "verify_manifest",
]
