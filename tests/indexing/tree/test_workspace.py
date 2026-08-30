from __future__ import annotations

import json

import pytest

from nianlun.indexing.tree.workspace import write_workspace_doc


def _document() -> dict:
    return {
        "type": "md",
        "doc_name": "report.md",
        "doc_description": "",
        "path": "/tmp/report.md",
        "line_count": 1,
        "structure": [],
    }


def test_write_workspace_doc_writes_document_and_manifest_atomically(tmp_path):
    write_workspace_doc(tmp_path, "doc-1", _document())

    assert json.loads((tmp_path / "doc-1.json").read_text()) == _document()
    assert (
        json.loads((tmp_path / "_meta.json").read_text())["doc-1"]["doc_name"]
        == "report.md"
    )


def test_write_workspace_doc_rejects_corrupt_manifest(tmp_path):
    (tmp_path / "_meta.json").write_text("{invalid", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest 不可读"):
        write_workspace_doc(tmp_path, "doc-1", _document())
