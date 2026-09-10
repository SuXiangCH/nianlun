from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nianlun.knowledgebase.core import KnowledgeBase
from nianlun.knowledgebase.local_scan_retriever import LocalScanNodeRetriever


def test_local_scan_uses_revision_artifacts_and_preserves_node_location(
    tmp_path: Path,
) -> None:
    tree = {
        "id": "doc-1",
        "doc_name": "报告.md",
        "doc_description": "年度经营报告",
        "type": "md",
        "line_count": 3,
        "structure": [
            {
                "title": "收入",
                "node_id": "0001",
                "line_num": 1,
                "text": "# 收入\n本年收入显著增长",
                "summary": "收入增长",
                "nodes": [],
            }
        ],
    }
    tree_raw = json.dumps(tree, ensure_ascii=False).encode()
    content = b"# revenue\nbody"
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/tree.json").write_bytes(tree_raw)
    (tmp_path / "artifacts/full.md").write_bytes(content)
    snapshot = tmp_path / "snapshots/r1"
    snapshot.mkdir(parents=True)
    manifest = {
        "schema_version": 2,
        "knowledge_base_id": "kb-1",
        "content_version": 1,
        "documents": [
            {
                "document_id": "doc-1",
                "index_relpath": "artifacts/tree.json",
                "content_relpath": "artifacts/full.md",
                "generation": 1,
                "doc_name": "报告.md",
                "doc_description": "年度经营报告",
                "line_count": 3,
                "type": "md",
            }
        ],
        "artifact_files": [
            {
                "relpath": relpath,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for relpath, raw in (
                ("artifacts/tree.json", tree_raw),
                ("artifacts/full.md", content),
            )
        ],
    }
    manifest_raw = json.dumps(manifest, ensure_ascii=False).encode()
    (snapshot / "manifest.json").write_bytes(manifest_raw)
    retriever = LocalScanNodeRetriever(
        workspace_dir=tmp_path,
        snapshot_relpath="snapshots/r1",
        snapshot_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        knowledge_base_id="kb-1",
        content_version=1,
    )
    knowledge_base = KnowledgeBase(
        tmp_path,
        full_text_retriever=retriever,
        snapshot_relpath="snapshots/r1",
        snapshot_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        knowledge_base_id="kb-1",
        content_version=1,
    )

    result = json.loads(knowledge_base.search_document_nodes("收入增长"))

    assert result["documents"][0]["doc_id"] == "doc-1"
    assert result["documents"][0]["node_hints"][0] == {
        "node_id": "0001",
        "title": "收入",
        "line_num": 1,
        "summary": "收入增长",
        "summary_truncated": False,
    }
