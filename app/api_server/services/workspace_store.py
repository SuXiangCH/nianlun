"""Atomic workspace writes owned by the API layer."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported deployment is POSIX
    fcntl = None


_thread_locks: dict[Path, threading.RLock] = {}
_thread_locks_guard = threading.Lock()
TREE_BUILD_OPTIONS_FILENAME = ".tree-build-options.json"


@dataclass(frozen=True, slots=True)
class TreeBuildOptions:
    """Persisted tree-build behavior for one API-owned knowledge-base workspace."""

    subtree_folding_enabled: bool = False
    min_subtree_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.subtree_folding_enabled:
            return
        if (
            isinstance(self.min_subtree_tokens, bool)
            or not isinstance(self.min_subtree_tokens, int)
            or self.min_subtree_tokens <= 0
        ):
            raise ValueError("启用子树折叠时 min_subtree_tokens 必须为正整数")


LEGACY_TREE_BUILD_OPTIONS = TreeBuildOptions()
NEW_KNOWLEDGE_BASE_TREE_BUILD_OPTIONS = TreeBuildOptions(
    subtree_folding_enabled=True,
    min_subtree_tokens=1200,
)


def _lock_for(path: Path) -> threading.RLock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(path, threading.RLock())


@contextmanager
def workspace_lock(workspace: Path) -> Generator[None, None, None]:
    """Serialize workspace writes across threads and POSIX processes."""
    lock_path = workspace / ".api-server.lock"
    lock_path.touch(exist_ok=True)
    with _lock_for(lock_path):
        handle = lock_path.open("r+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


class WorkspaceArtifactStore:
    """Persist source, document artifact and manifest as one locked sequence."""

    @staticmethod
    def atomic_write(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def write_tree_build_options(
        cls, workspace: Path, options: TreeBuildOptions
    ) -> None:
        payload = {
            "version": 1,
            "subtree_folding_enabled": options.subtree_folding_enabled,
            "min_subtree_tokens": options.min_subtree_tokens,
        }
        cls.atomic_write(
            workspace / TREE_BUILD_OPTIONS_FILENAME,
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

    @staticmethod
    def read_tree_build_options(workspace: Path) -> TreeBuildOptions:
        path = workspace / TREE_BUILD_OPTIONS_FILENAME
        if not path.exists():
            # Workspaces created before this option existed retain their tree.
            return LEGACY_TREE_BUILD_OPTIONS
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"知识库树构建配置不可读: {path}") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError(f"知识库树构建配置格式无效: {path}")
        enabled = payload.get("subtree_folding_enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"知识库树构建配置格式无效: {path}")
        try:
            return TreeBuildOptions(
                subtree_folding_enabled=enabled,
                min_subtree_tokens=payload.get("min_subtree_tokens"),
            )
        except ValueError as exc:
            raise ValueError(f"知识库树构建配置格式无效: {path}") from exc

    @staticmethod
    def _read_manifest(workspace: Path) -> dict[str, dict[str, Any]]:
        path = workspace / "_meta.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"知识库 manifest 不可读: {path}") from exc
        if not isinstance(payload, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in payload.items()
        ):
            raise ValueError(f"知识库 manifest 格式无效: {path}")
        return payload

    def write_document(
        self,
        workspace: Path,
        document_id: str,
        source_relpath: str,
        source_content: bytes,
        document: dict[str, Any],
    ) -> tuple[int, str]:
        """Atomically commit source/artifact and return count and artifact hash."""
        source_path = workspace / source_relpath
        artifact_relpath = f"{document_id}.json"
        artifact_path = workspace / artifact_relpath
        source_path.parent.mkdir(parents=True, exist_ok=True)

        document["id"] = document_id
        document["path"] = source_relpath
        artifact_content = json.dumps(
            document, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")
        self.atomic_write(source_path, source_content)
        self.atomic_write(artifact_path, artifact_content)

        metadata = self._read_manifest(workspace)
        metadata[document_id] = {
            "type": document["type"],
            "doc_name": document["doc_name"],
            "doc_description": document["doc_description"],
            "path": source_relpath,
            "line_count": document["line_count"],
        }
        manifest_content = json.dumps(
            metadata, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")
        self.atomic_write(workspace / "_meta.json", manifest_content)
        return len(metadata), hashlib.sha256(artifact_content).hexdigest()

    @classmethod
    def discard_document(
        cls,
        workspace: Path,
        document_id: str,
        source_relpath: str | None = None,
    ) -> None:
        """Remove a partially committed document while its workspace is locked."""
        artifact_path = workspace / f"{document_id}.json"
        artifact_path.unlink(missing_ok=True)

        manifest_path = workspace / "_meta.json"
        try:
            metadata = cls._read_manifest(workspace)
        except ValueError:
            metadata = None

        if metadata is not None:
            entry = metadata.pop(document_id, None)
            if entry is not None and source_relpath is None:
                candidate = entry.get("path")
                if isinstance(candidate, str):
                    source_relpath = candidate
            if entry is not None:
                manifest_content = json.dumps(
                    metadata, ensure_ascii=False, indent=2, sort_keys=True
                ).encode("utf-8")
                cls.atomic_write(manifest_path, manifest_content)

        if source_relpath:
            workspace_root = workspace.resolve()
            source_path = (workspace / source_relpath).resolve()
            try:
                source_path.relative_to(workspace_root)
            except ValueError:
                return
            source_path.unlink(missing_ok=True)

    @classmethod
    def delete_document(
        cls,
        workspace: Path,
        document_id: str,
        source_relpath: str | None = None,
        artifact_relpaths: list[str] | None = None,
    ) -> None:
        """Remove a document's local files and manifest entry.

        Absolute paths can occur in trusted legacy manifests. They are removed
        from the local catalog, but are deliberately never deleted here because
        they are outside the API workspace.
        """
        metadata = cls._read_manifest(workspace)
        entry = metadata.pop(document_id, None)
        candidates: list[str] = []
        if source_relpath:
            candidates.append(source_relpath)
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
            candidates.append(str(entry["path"]))
        candidates.extend(artifact_relpaths or [])

        workspace_root = workspace.resolve()
        for stored in candidates:
            if Path(stored).is_absolute():
                continue
            path = (workspace / stored).resolve()
            try:
                path.relative_to(workspace_root)
            except ValueError:
                continue
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)

        parsed_dir = (workspace / "parsed" / document_id).resolve()
        try:
            parsed_dir.relative_to(workspace_root)
        except ValueError:
            parsed_dir = None
        if parsed_dir is not None and parsed_dir.is_dir():
            shutil.rmtree(parsed_dir)

        (workspace / f"{document_id}.json").unlink(missing_ok=True)
        manifest_content = json.dumps(
            metadata, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")
        cls.atomic_write(workspace / "_meta.json", manifest_content)

    @classmethod
    def document_count(cls, workspace: Path) -> int:
        metadata = cls._read_manifest(workspace)
        workspace_root = workspace.resolve()
        incomplete: list[str] = []
        for document_id, item in metadata.items():
            if not (workspace / f"{document_id}.json").is_file():
                incomplete.append(document_id)
                continue
            source_relpath = item.get("path")
            if not isinstance(source_relpath, str):
                # Legacy manifests may not record the original source file.
                continue
            if Path(source_relpath).is_absolute():
                # CLI-generated legacy manifests can point at a parsed artifact
                # outside the API workspace. The manifest is trusted here; the
                # only requirement is that the referenced artifact still exists.
                source_path = Path(source_relpath)
            else:
                source_path = (workspace / source_relpath).resolve()
                try:
                    source_path.relative_to(workspace_root)
                except ValueError:
                    incomplete.append(document_id)
                    continue
            if not source_path.is_file():
                incomplete.append(document_id)
        if incomplete:
            raise ValueError(f"manifest 包含不完整文档: {incomplete[:10]}")
        return len(metadata)


__all__ = [
    "LEGACY_TREE_BUILD_OPTIONS",
    "NEW_KNOWLEDGE_BASE_TREE_BUILD_OPTIONS",
    "TREE_BUILD_OPTIONS_FILENAME",
    "TreeBuildOptions",
    "WorkspaceArtifactStore",
    "workspace_lock",
]
