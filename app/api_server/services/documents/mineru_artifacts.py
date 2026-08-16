"""Safe extraction and selection helpers for MinerU result archives."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from app.api_server.integrations.mineru import MineruError
from app.api_server.services.workspace_store import WorkspaceArtifactStore


def extract_result_archive(
    content: bytes, destination: Path, *, max_member_bytes: int
) -> list[Path]:
    """Extract a MinerU ZIP while rejecting archive path traversal."""
    paths: list[Path] = []
    destination_root = destination.resolve()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for info in archive.infolist():
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise MineruError("MinerU 解析 ZIP 包含非法路径")
            if info.is_dir():
                continue
            if info.file_size > max_member_bytes:
                raise MineruError("MinerU 解析 ZIP 单文件超过大小限制")
            target = (destination / member).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError as exc:
                raise MineruError("MinerU 解析 ZIP 路径越界") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            WorkspaceArtifactStore.atomic_write(target, archive.read(info))
            paths.append(target)
    return paths


def select_markdown_result(
    files: list[Path], document: dict[str, Any], task: dict[str, Any]
) -> Path | None:
    """Select the MinerU Markdown result for SaaS and self-hosted responses."""
    if task.get("api_mode") != "self_hosted":
        return next((path for path in files if path.name.lower() == "full.md"), None)
    expected_name = f"{Path(str(document['original_filename'])).stem}.md".lower()
    named = next((path for path in files if path.name.lower() == expected_name), None)
    if named is not None:
        return named
    markdown_files = [path for path in files if path.suffix.lower() == ".md"]
    return markdown_files[0] if len(markdown_files) == 1 else None
