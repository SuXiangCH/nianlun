from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app.api_server.services.documents.pdf_chunks import PdfChunkError, split_pdf


def _encrypted_pdf(page_count: int, password: str) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    writer.encrypt(password)
    writer.write(output)
    return output.getvalue()


def test_empty_password_encrypted_pdf_is_rewritten_without_encryption(
    tmp_path: Path,
) -> None:
    chunks = split_pdf(
        _encrypted_pdf(1, ""),
        workspace=tmp_path,
        document_id="doc-1",
        generation=1,
        page_ranges="",
        max_pages=180,
        max_chunks=100,
    )

    assert len(chunks) == 1
    assert chunks[0].relpath == "staging/doc-1/g1/parse/chunks/0000.pdf"
    rewritten = PdfReader(tmp_path / str(chunks[0].relpath))
    assert not rewritten.is_encrypted
    assert len(rewritten.pages) == 1


def test_empty_password_encrypted_long_pdf_is_decrypted_and_chunked(
    tmp_path: Path,
) -> None:
    chunks = split_pdf(
        _encrypted_pdf(201, ""),
        workspace=tmp_path,
        document_id="doc-1",
        generation=1,
        page_ranges="",
        max_pages=180,
        max_chunks=100,
    )

    assert [(chunk.source_page_start, chunk.source_page_end) for chunk in chunks] == [
        (1, 180),
        (181, 201),
    ]
    for chunk in chunks:
        rewritten = PdfReader(tmp_path / str(chunk.relpath))
        assert not rewritten.is_encrypted


def test_password_protected_pdf_reports_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(PdfChunkError, match="PDF 需要密码，请解除密码后重新上传"):
        split_pdf(
            _encrypted_pdf(1, "secret"),
            workspace=tmp_path,
            document_id="doc-1",
            generation=1,
            page_ranges="",
            max_pages=180,
            max_chunks=100,
        )

    assert not (tmp_path / "staging").exists()
