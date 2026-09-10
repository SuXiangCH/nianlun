"""Physical PDF chunk planning and durable generation-scoped writes."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from pypdf import PasswordType, PdfReader, PdfWriter

from app.api_server.services.workspace_store import WorkspaceArtifactStore


class PdfChunkError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PdfChunk:
    chunk_index: int
    source_page_start: int | None
    source_page_end: int | None
    relpath: str | None
    sha256: str
    size_bytes: int


def parse_page_ranges(value: str, total_pages: int) -> list[tuple[int, int]]:
    if total_pages < 1:
        raise PdfChunkError("PDF 没有可解析页面")
    if not value.strip():
        return [(1, total_pages)]
    pages: set[int] = set()
    try:
        for part in value.split(","):
            bounds = part.strip().split("-", 1)
            start = int(bounds[0])
            end = int(bounds[1]) if len(bounds) == 2 else start
            if start < 1 or end < start:
                raise ValueError
            pages.update(range(max(start, 1), min(end, total_pages) + 1))
    except ValueError as exc:
        raise PdfChunkError("page_ranges 格式无效") from exc
    if not pages:
        raise PdfChunkError("page_ranges 与 PDF 页数没有交集")
    ordered = sorted(pages)
    ranges: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append((start, previous))
        start = previous = page
    ranges.append((start, previous))
    return ranges


def plan_chunks(
    page_ranges: list[tuple[int, int]], max_pages: int, max_chunks: int
) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    for range_start, range_end in page_ranges:
        start = range_start
        while start <= range_end:
            end = min(start + max_pages - 1, range_end)
            chunks.append((start, end))
            start = end + 1
    if len(chunks) > max_chunks:
        raise PdfChunkError(f"PDF 分段数超过限制: {len(chunks)} > {max_chunks}")
    return chunks


def split_pdf(
    content: bytes,
    *,
    workspace: Path,
    document_id: str,
    generation: int,
    page_ranges: str,
    max_pages: int,
    max_chunks: int,
) -> list[PdfChunk]:
    try:
        reader = PdfReader(io.BytesIO(content))
        encrypted = reader.is_encrypted
        if encrypted:
            try:
                password_type = reader.decrypt("")
            except Exception as exc:
                raise PdfChunkError("PDF 需要密码，请解除密码后重新上传") from exc
            if password_type == PasswordType.NOT_DECRYPTED:
                raise PdfChunkError("PDF 需要密码，请解除密码后重新上传")
        total_pages = len(reader.pages)
    except PdfChunkError:
        raise
    except Exception as exc:
        raise PdfChunkError("PDF 文件损坏或格式无效") from exc
    ranges = parse_page_ranges(page_ranges, total_pages)
    planned = plan_chunks(ranges, max_pages, max_chunks)
    if not encrypted and planned == [(1, total_pages)]:
        return [
            PdfChunk(
                chunk_index=0,
                source_page_start=1,
                source_page_end=total_pages,
                relpath=None,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            )
        ]
    chunks: list[PdfChunk] = []
    base = Path("staging") / document_id / f"g{generation}" / "parse" / "chunks"
    for chunk_index, (start, end) in enumerate(planned):
        writer = PdfWriter()
        for page_number in range(start, end + 1):
            writer.add_page(reader.pages[page_number - 1])
        output = io.BytesIO()
        writer.write(output)
        raw = output.getvalue()
        relpath = str(base / f"{chunk_index:04d}.pdf")
        path = workspace / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if (
            path.exists()
            and hashlib.sha256(path.read_bytes()).digest()
            != hashlib.sha256(raw).digest()
        ):
            raise PdfChunkError(f"PDF 分段产物冲突: {chunk_index}")
        if not path.exists():
            WorkspaceArtifactStore.atomic_write(path, raw)
        chunks.append(
            PdfChunk(
                chunk_index=chunk_index,
                source_page_start=start,
                source_page_end=end,
                relpath=relpath,
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
            )
        )
    return chunks


__all__ = ["PdfChunk", "PdfChunkError", "parse_page_ranges", "plan_chunks", "split_pdf"]
