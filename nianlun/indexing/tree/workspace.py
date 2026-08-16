"""Workspace document persistence shared by API services and indexing jobs."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from nianlun.indexing.tree.pipeline import build_md_index_sync


_UUID_SUFFIX = re.compile(
    r"(.+)-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def derive_doc_name(file_path: str) -> str | None:
    """Derive the original name from a dataset ``full.md`` path."""
    path = Path(file_path)
    if path.name == "full.md" and path.parent.name != "datasets":
        match = _UUID_SUFFIX.match(path.parent.name)
        if match:
            return match.group(1)
    return None


def build_workspace_doc(
    md_path: str,
    model: str | None = None,
    no_summary: bool = False,
    atx_only: bool = True,
    llm: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Build one Markdown index and normalize it to the workspace contract."""
    result = build_md_index_sync(
        md_path,
        model=model,
        llm=llm,
        add_node_summary=not no_summary,
        add_doc_description=not no_summary,
        add_node_text=True,
        add_node_id=True,
        atx_only=atx_only,
    )
    doc_id = str(uuid.uuid4())
    doc = {
        "id": doc_id,
        "type": "md",
        "path": os.path.abspath(md_path),
        "doc_name": derive_doc_name(md_path) or result["doc_name"],
        "doc_description": result["doc_description"] or "",
        "line_count": result["line_count"],
        "structure": result["structure"],
    }
    return doc_id, doc


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
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


def write_workspace_doc(workspace: Path | str, doc_id: str, doc: dict[str, Any]) -> None:
    """Write one document and merge its metadata into ``_meta.json``."""
    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(workspace_path / f"{doc_id}.json", doc)
    meta_path = workspace_path / "_meta.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        metadata = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"workspace manifest 不可读: {meta_path}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"workspace manifest 格式无效: {meta_path}")
    metadata[doc_id] = {
        "type": doc["type"],
        "doc_name": doc["doc_name"],
        "doc_description": doc["doc_description"],
        "path": doc["path"],
        "line_count": doc["line_count"],
    }
    _atomic_write_json(meta_path, metadata)


__all__ = ["build_workspace_doc", "derive_doc_name", "write_workspace_doc"]
