"""预建树导入链路（服务层 + CLI）的测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.api_server.apis.v1.schemas import KnowledgeBaseCreateRequest
from app.api_server.config import ApiServerSettings
from app.api_server.import_datasets import main as import_main
from app.api_server.services.container import build_services


def _settings(tmp_path: Path) -> ApiServerSettings:
    return ApiServerSettings(
        data_dir=tmp_path / "api",
        workspace_root=tmp_path / "workspaces",
        # 脱离 Milvus；导入本身不依赖 FTS。
        fts_enabled=False,
    )


def _prebuilt_tree() -> dict[str, Any]:
    return {
        "type": "md",
        "doc_name": "预建测试文档.pdf",
        "doc_description": "预建描述",
        "line_count": 3,
        "structure": [
            {
                "title": "章节",
                "node_id": "0001",
                "text": "# 章节\n正文",
                "line_num": 1,
                "summary": "预建摘要",
                "nodes": [],
            }
        ],
    }


def test_upload_with_prebuilt_document_preserves_tree(tmp_path: Path) -> None:
    services = build_services(_settings(tmp_path))
    kb = services.knowledge_bases.create(KnowledgeBaseCreateRequest(name="导入测试"))
    content = "# 章节\n正文\n".encode("utf-8")

    response = services.documents.upload(
        str(kb.id),
        "预建测试文档.md",
        content,
        "text/markdown",
        None,
        prebuilt_document=_prebuilt_tree(),
    )
    assert response.idempotent_replay is False
    doc_id = str(response.document_id)

    record = services.documents.repository.get_document(str(kb.id), doc_id)
    assert record is not None and record["status"] == "ready"
    assert record["parser"] == "native_markdown"

    workspace = Path(
        str(services.knowledge_bases.require_record(str(kb.id))["workspace_dir"])
    )
    tree = json.loads((workspace / f"{doc_id}.json").read_text(encoding="utf-8"))
    # 预建树的摘要原样保留；path/doc_name 覆写为入库后的真实值。
    assert tree["structure"][0]["summary"] == "预建摘要"
    assert tree["path"].startswith("sources/")
    assert tree["doc_name"] == "预建测试文档.md"

    meta = json.loads((workspace / "_meta.json").read_text(encoding="utf-8"))
    assert doc_id in meta

    # 相同内容重复导入按哈希幂等跳过。
    replay = services.documents.upload(
        str(kb.id),
        "预建测试文档.md",
        content,
        "text/markdown",
        None,
        prebuilt_document=_prebuilt_tree(),
    )
    assert replay.idempotent_replay is True


def _write_dataset_dir(root: Path) -> Path:
    datasets = root / "datasets"
    doc_dir = datasets / "预建测试文档.pdf-00000000-0000-0000-0000-000000000001"
    doc_dir.mkdir(parents=True)
    (doc_dir / "full.md").write_text("# 章节\n正文\n", encoding="utf-8")
    workspace = datasets / "workspace"
    workspace.mkdir()
    tree = _prebuilt_tree()
    tree["path"] = f"datasets/{doc_dir.name}/full.md"
    (workspace / "doc-1.json").write_text(
        json.dumps(tree, ensure_ascii=False), encoding="utf-8"
    )
    (workspace / "_meta.json").write_text(
        json.dumps(
            {"doc-1": {"doc_name": "预建测试文档.pdf", "path": tree["path"]}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return datasets


def test_import_cli_end_to_end(tmp_path: Path) -> None:
    datasets = _write_dataset_dir(tmp_path)
    settings = _settings(tmp_path)

    code = import_main(
        ["--datasets-dir", str(datasets), "--knowledge-base", "财报数据集", "--create"],
        settings=settings,
    )
    assert code == 0

    services = build_services(settings)
    kb = services.knowledge_bases.list()[0]
    assert kb.name == "财报数据集"
    docs = services.documents.repository.list_documents(str(kb.id))
    assert len(docs) == 1
    assert docs[0]["status"] == "ready"
    assert docs[0]["original_filename"] == "预建测试文档.md"

    # 重复执行整批导入：幂等跳过，不新增文档。
    code = import_main(
        ["--datasets-dir", str(datasets), "--knowledge-base", "财报数据集"],
        settings=settings,
    )
    assert code == 0
    assert len(services.documents.repository.list_documents(str(kb.id))) == 1
