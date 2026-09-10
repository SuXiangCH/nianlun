"""Validated read-only views over legacy and revisioned knowledge-base workspaces."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorkspaceViewError(ValueError):
    """A workspace manifest or referenced artifact violates the read contract."""


class WorkspaceSnapshotHandle:
    """One ref-counted claim preventing deletion of a snapshot directory."""

    def __init__(self, registry: WorkspaceSnapshotRegistry, snapshot: Path) -> None:
        self._registry = registry
        self.snapshot = snapshot
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._registry.release(self.snapshot)

    def __enter__(self) -> WorkspaceSnapshotHandle:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


class WorkspaceSnapshotRegistry:
    """Serialize reader refcounts with snapshot GC decisions in one process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._references: dict[Path, int] = {}

    def acquire(self, snapshot: Path) -> WorkspaceSnapshotHandle:
        resolved = snapshot.resolve()
        with self._lock:
            self._references[resolved] = self._references.get(resolved, 0) + 1
        return WorkspaceSnapshotHandle(self, resolved)

    def release(self, snapshot: Path) -> None:
        resolved = snapshot.resolve()
        with self._lock:
            count = self._references.get(resolved, 0)
            if count <= 1:
                self._references.pop(resolved, None)
            else:
                self._references[resolved] = count - 1

    def is_referenced(self, snapshot: Path) -> bool:
        with self._lock:
            return self._references.get(snapshot.resolve(), 0) > 0

    def run_if_unreferenced(self, snapshot: Path, action: Callable[[], None]) -> bool:
        """Run a GC mutation while acquisition is excluded by the same lock."""
        with self._lock:
            if self._references.get(snapshot.resolve(), 0) > 0:
                return False
            action()
            return True


workspace_snapshot_registry = WorkspaceSnapshotRegistry()


@dataclass(frozen=True, slots=True)
class _ArtifactRecord:
    relpath: str
    size_bytes: int
    sha256: str


class WorkspaceView:
    """Resolve document metadata and immutable tree artifacts for one revision."""

    def __init__(
        self,
        workspace: Path,
        meta: dict[str, dict[str, Any]],
        index_paths: dict[str, str],
        artifact_records: dict[str, _ArtifactRecord] | None = None,
        snapshot_handle: WorkspaceSnapshotHandle | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.meta = meta
        self._index_paths = index_paths
        self._artifact_records = artifact_records or {}
        self._snapshot_handle = snapshot_handle

    @classmethod
    def legacy(cls, workspace: Path | str) -> WorkspaceView:
        root = Path(workspace).resolve()
        meta_path = root / "_meta.json"
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceViewError(f"workspace manifest 不可读: {meta_path}") from exc
        if not isinstance(payload, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in payload.items()
        ):
            raise WorkspaceViewError(f"workspace manifest 格式无效: {meta_path}")
        return cls(
            root,
            payload,
            {document_id: f"{document_id}.json" for document_id in payload},
        )

    @classmethod
    def revision(
        cls,
        workspace: Path | str,
        snapshot_relpath: str,
        manifest_sha256: str,
        *,
        knowledge_base_id: str,
        content_version: int,
    ) -> WorkspaceView:
        root = Path(workspace).resolve()
        snapshot_dir = _resolve_confined(root, snapshot_relpath)
        handle = workspace_snapshot_registry.acquire(snapshot_dir)
        manifest_path = _resolve_confined(root, f"{snapshot_relpath}/manifest.json")
        try:
            raw = manifest_path.read_bytes()
        except OSError as exc:
            handle.close()
            raise WorkspaceViewError(
                f"snapshot manifest 不可读: {manifest_path}"
            ) from exc
        if hashlib.sha256(raw).hexdigest() != manifest_sha256:
            handle.close()
            raise WorkspaceViewError(f"snapshot manifest hash 不匹配: {manifest_path}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            handle.close()
            raise WorkspaceViewError(
                f"snapshot manifest JSON 无效: {manifest_path}"
            ) from exc
        try:
            if not isinstance(payload, dict) or payload.get("schema_version") != 2:
                raise WorkspaceViewError("snapshot manifest schema_version 必须为 2")
            if payload.get("knowledge_base_id") != knowledge_base_id:
                raise WorkspaceViewError("snapshot manifest knowledge_base_id 不匹配")
            if payload.get("content_version") != content_version:
                raise WorkspaceViewError("snapshot manifest content_version 不匹配")

            raw_files = payload.get("artifact_files")
            raw_documents = payload.get("documents")
            if not isinstance(raw_files, list) or not isinstance(raw_documents, list):
                raise WorkspaceViewError(
                    "snapshot manifest 缺少 documents/artifact_files"
                )
            records: dict[str, _ArtifactRecord] = {}
            for raw_record in raw_files:
                if not isinstance(raw_record, dict):
                    raise WorkspaceViewError("snapshot artifact_files 条目无效")
                relpath = raw_record.get("relpath")
                size_bytes = raw_record.get("size_bytes")
                sha256 = raw_record.get("sha256")
                if (
                    not isinstance(relpath, str)
                    or isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                    or size_bytes < 0
                    or not isinstance(sha256, str)
                    or len(sha256) != 64
                    or relpath in records
                ):
                    raise WorkspaceViewError("snapshot artifact_files 条目无效")
                _resolve_confined(root, relpath)
                records[relpath] = _ArtifactRecord(relpath, size_bytes, sha256)

            meta: dict[str, dict[str, Any]] = {}
            index_paths: dict[str, str] = {}
            for raw_document in raw_documents:
                if not isinstance(raw_document, dict):
                    raise WorkspaceViewError("snapshot documents 条目无效")
                document_id = raw_document.get("document_id")
                index_relpath = raw_document.get("index_relpath")
                generation = raw_document.get("generation")
                if (
                    not isinstance(document_id, str)
                    or not document_id
                    or document_id in meta
                    or not isinstance(index_relpath, str)
                    or isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation < 1
                ):
                    raise WorkspaceViewError("snapshot documents 条目无效")
                for field in (
                    "index_relpath",
                    "content_relpath",
                    "page_map_relpath",
                    "source_relpath",
                ):
                    relpath = raw_document.get(field)
                    if relpath is None and field != "index_relpath":
                        continue
                    if not isinstance(relpath, str) or relpath not in records:
                        raise WorkspaceViewError(
                            f"snapshot document 引用了未登记的 artifact: {field}"
                        )
                    _resolve_confined(root, relpath)
                index_paths[document_id] = index_relpath
                meta[document_id] = {
                    "type": raw_document.get("type") or "md",
                    "doc_name": raw_document.get("doc_name") or "unknown",
                    "doc_description": raw_document.get("doc_description") or "",
                    "path": raw_document.get("content_relpath") or "",
                    "line_count": raw_document.get("line_count") or 0,
                }
            return cls(root, meta, index_paths, records, handle)
        except Exception:
            handle.close()
            raise

    def close(self) -> None:
        handle = self._snapshot_handle
        if handle is not None:
            handle.close()
            self._snapshot_handle = None

    def load_document(self, document_id: str) -> dict[str, Any]:
        if document_id not in self.meta:
            raise KeyError(document_id)
        relpath = self._index_paths[document_id]
        raw = self._read_artifact(relpath)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WorkspaceViewError(
                f"文档 tree artifact JSON 无效: {relpath}"
            ) from exc
        if not isinstance(payload, dict):
            raise WorkspaceViewError(f"文档 tree artifact 格式无效: {relpath}")
        return payload

    def read_content(self, document_id: str) -> str:
        if document_id not in self.meta:
            raise KeyError(document_id)
        relpath = self.meta[document_id].get("path")
        if not isinstance(relpath, str) or not relpath:
            raise WorkspaceViewError(f"文档缺少 content artifact: {document_id}")
        try:
            return self._read_artifact(relpath).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceViewError(
                f"文档 content artifact 不是 UTF-8: {relpath}"
            ) from exc

    def _read_artifact(self, relpath: str) -> bytes:
        path = _resolve_confined(self.workspace, relpath)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise WorkspaceViewError(f"文档 tree artifact 不可读: {relpath}") from exc
        record = self._artifact_records.get(relpath)
        if record is not None and (
            len(raw) != record.size_bytes
            or hashlib.sha256(raw).hexdigest() != record.sha256
        ):
            raise WorkspaceViewError(f"文档 artifact hash 不匹配: {relpath}")
        return raw


def _resolve_confined(workspace: Path, relpath: str) -> Path:
    if not relpath or Path(relpath).is_absolute():
        raise WorkspaceViewError(f"workspace 引用了非法路径: {relpath!r}")
    candidate = (workspace / relpath).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise WorkspaceViewError(f"workspace 引用了目录外路径: {relpath!r}") from exc
    return candidate


__all__ = [
    "WorkspaceSnapshotHandle",
    "WorkspaceSnapshotRegistry",
    "WorkspaceView",
    "WorkspaceViewError",
    "workspace_snapshot_registry",
]
