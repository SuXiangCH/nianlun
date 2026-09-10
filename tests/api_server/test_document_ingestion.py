from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

import app.api_server.services.document_ingestion_service as ingestion_module
import app.api_server.services.model_config_service as model_config_service
from app.api_server.config import ApiServerSettings
from app.api_server.integrations.mineru import (
    MineruBatchResult,
    MineruClient,
    MineruError,
    MineruUploadTicket,
    SelfHostedMineruClient,
)
from app.api_server.services.documents.mineru_artifacts import (
    extract_result_archive,
    select_markdown_result,
)
from app.api_server.services.documents.mineru_tasks import build_parser_options
from app.api_server.services.documents.pdf_chunks import PdfChunkError
from app.api_server.main import create_app


def _settings(tmp_path: Path) -> ApiServerSettings:
    return ApiServerSettings(
        data_dir=tmp_path / "api",
        workspace_root=tmp_path / "workspaces",
        fts_enabled=False,
        mineru_poll_interval_seconds=0,
        mineru_poll_timeout_seconds=10,
    )


def _result_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "result/full.md",
            "# PDF 标题\n\n## 1. 投资要点\n\n## 1.1 盈利预测\n\n"
            "这是 MinerU 解析后的完整内容。\n\n## 2. 风险提示",
        )
        archive.writestr("result/content_list.json", '[{"type": "text"}]')
    return output.getvalue()


def _pdf_bytes(page_count: int = 1) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def _self_hosted_result_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("private.md", "# 私有 PDF 标题\n\n这是私有 MinerU 的结果。")
    return output.getvalue()


def _missing_markdown_result_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("result/metadata.json", "{}")
    return output.getvalue()


def _resource_result_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("result/full.md", "# PDF\n\n![](images/chart.png)")
        archive.writestr("result/images/chart.png", b"chart")
        archive.writestr(
            "result/content_list.json",
            json.dumps(
                [
                    {"type": "text", "text": "PDF", "page_idx": 0},
                    {
                        "type": "image",
                        "img_path": "images/chart.png",
                        "page_idx": 0,
                    },
                ]
            ),
        )
    return output.getvalue()


def test_result_archive_rejects_path_traversal(tmp_path: Path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../outside.md", "not allowed")

    with pytest.raises(MineruError, match="非法路径"):
        extract_result_archive(
            output.getvalue(), tmp_path / "parsed", max_member_bytes=1024
        )


def test_result_archive_rejects_entry_count_compression_ratio_and_quota(
    tmp_path: Path,
) -> None:
    entries = io.BytesIO()
    with zipfile.ZipFile(entries, "w") as archive:
        archive.writestr("one.md", "one")
        archive.writestr("two.md", "two")
    with pytest.raises(MineruError, match="文件项数量"):
        extract_result_archive(
            entries.getvalue(),
            tmp_path / "entries",
            max_member_bytes=1024,
            max_entries=1,
        )

    compressed = io.BytesIO()
    with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.md", "0" * 10_000)
    with pytest.raises(MineruError, match="压缩比"):
        extract_result_archive(
            compressed.getvalue(),
            tmp_path / "ratio",
            max_member_bytes=20_000,
            max_compression_ratio=2,
        )

    with pytest.raises(MineruError, match="配额不足"):
        extract_result_archive(
            entries.getvalue(),
            tmp_path / "quota",
            max_member_bytes=1024,
            reserve_extracted_bytes=lambda _size: False,
        )
    assert not (tmp_path / "quota/one.md").exists()


def test_mineru_result_download_streams_to_bounded_file(tmp_path: Path) -> None:
    payload = _result_zip()
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=payload)
        )
    )
    client = MineruClient("https://mineru.test", "secret", http_client=http_client)
    destination = tmp_path / "result.zip.part"
    reservations: list[int] = []

    written = client.download_result_to(
        "https://result.test/file.zip",
        destination,
        max_bytes=len(payload),
        reserve_bytes=lambda size: reservations.append(size) is None,
    )

    assert written == len(payload)
    assert destination.read_bytes() == payload
    assert reservations[-1] == len(payload)

    with pytest.raises(MineruError, match="大小限制"):
        client.download_result_to(
            "https://result.test/file.zip",
            tmp_path / "too-large.part",
            max_bytes=len(payload) - 1,
        )
    assert not (tmp_path / "too-large.part").exists()


def test_self_hosted_result_selects_single_markdown_file(tmp_path: Path) -> None:
    markdown = tmp_path / "converted.md"
    markdown.write_text("# converted", encoding="utf-8")

    selected = select_markdown_result(
        [markdown],
        {"original_filename": "source.pdf"},
        {"api_mode": "self_hosted"},
    )

    assert selected == markdown


def test_parser_options_limit_page_ranges_to_pdf() -> None:
    config = {
        "language": "ch",
        "is_ocr": True,
        "enable_table": True,
        "enable_formula": False,
        "page_ranges": "2-4",
    }

    assert build_parser_options(config, ".pdf")["page_ranges"] == "2-4"
    assert "page_ranges" not in build_parser_options(config, ".docx")


class FakeMineruClient:
    result = _result_zip()

    def __init__(self, _base_url: str, _api_key: str) -> None:
        pass

    def request_upload_url(self, **_kwargs: object) -> MineruUploadTicket:
        return MineruUploadTicket("batch-1", "https://upload.invalid/file")

    def upload_file(self, _upload_url: str, _content: bytes) -> None:
        return None

    def get_batch_result(self, _batch_id: str) -> MineruBatchResult:
        return MineruBatchResult(
            "done", "doc-1", "https://result.invalid/file.zip", 1, 1, None, None
        )

    def download_result(self, _result_url: str) -> bytes:
        return self.result

    def close(self) -> None:
        return None


class FakeSelfHostedMineruClient(FakeMineruClient):
    result = _self_hosted_result_zip()

    def submit_file(self, **_kwargs: object) -> str:
        return "private-task-1"

    def get_task_result(self, _task_id: str) -> MineruBatchResult:
        return MineruBatchResult("done", None, "private-task-1", 1, 1, None, None)


class MissingMarkdownMineruClient(FakeMineruClient):
    result = _missing_markdown_result_zip()


class ResourceMineruClient(FakeMineruClient):
    result = _resource_result_zip()


class RestartingSelfHostedMineruClient(FakeSelfHostedMineruClient):
    submit_count = 0
    query_count = 0

    def submit_file(self, **_kwargs: object) -> str:
        type(self).submit_count += 1
        return f"private-task-{self.submit_count}"

    def get_task_result(self, _task_id: str) -> MineruBatchResult:
        type(self).query_count += 1
        if type(self).query_count == 1:
            raise MineruError("task not found", code="HTTP_404")
        return MineruBatchResult("done", None, _task_id, 1, 1, None, None)


class SlowSubmissionMineruClient(FakeMineruClient):
    def request_upload_url(self, **kwargs: object) -> MineruUploadTicket:
        time.sleep(0.3)
        return super().request_upload_url(**kwargs)


class RetryMineruClient(FakeMineruClient):
    submit_count = 0
    poll_count = 0
    submissions_by_data_id: dict[str, int] = {}

    def request_upload_url(self, **kwargs: object) -> MineruUploadTicket:
        type(self).submit_count += 1
        data_id = str(kwargs["data_id"])
        data_submit_count = type(self).submissions_by_data_id.get(data_id, 0) + 1
        type(self).submissions_by_data_id[data_id] = data_submit_count
        return MineruUploadTicket(
            f"batch-{data_id}:s{data_submit_count}", "https://upload.invalid/file"
        )

    def get_batch_result(self, batch_id: str) -> MineruBatchResult:
        type(self).poll_count += 1
        if batch_id.endswith(":s1"):
            return MineruBatchResult(
                "failed", "doc-1", None, 0, 1, "TEMP", "temporary failure"
            )
        return super().get_batch_result(batch_id)


class ChunkTrackingMineruClient(FakeMineruClient):
    data_ids: list[str] = []

    def request_upload_url(self, **kwargs: object) -> MineruUploadTicket:
        data_id = str(kwargs["data_id"])
        type(self).data_ids.append(data_id)
        return MineruUploadTicket(
            f"batch-{data_id.rsplit(':', 1)[-1]}", "https://upload.invalid/file"
        )


class FakeSummaryLLM:
    def invoke(self, _prompt: str, **_kwargs: object) -> str:
        return "文档描述"

    async def ainvoke(self, prompt: str, **_kwargs: object) -> str:
        if prompt.startswith("You assign hierarchical levels"):
            headings = json.loads(prompt.split("headings=", 1)[1])
            return json.dumps(
                {
                    "levels": {
                        str(heading["id"]): heading["rule_level"]
                        for heading in headings
                    }
                }
            )
        return "文档描述" if "Document Structure:" in prompt else "节点摘要"


def _configure_parser(client: TestClient, *, api_mode: str = "saas_precision") -> None:
    response = client.post(
        "/api/v1/models",
        json={
            "kind": "parser",
            "name": "MinerU 私有部署" if api_mode == "self_hosted" else "MinerU SaaS",
            "model": None,
            "base_url": "https://mineru.test",
            "api_key": "test-token",
            "api_mode": api_mode,
            "model_version": "vlm",
            "language": "ch",
            "is_ocr": False,
            "enable_table": True,
            "enable_formula": True,
            "page_ranges": "",
            "is_default": True,
        },
    )
    assert response.status_code == 200


def test_self_hosted_mineru_client_uses_task_api_and_zip_result() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/tasks":
            return httpx.Response(200, json={"task_id": "private-task-1"})
        if request.url.path == "/tasks/private-task-1":
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path == "/tasks/private-task-1/result":
            return httpx.Response(200, content=_result_zip())
        return httpx.Response(404)

    client = SelfHostedMineruClient(
        "http://mineru.internal:8000",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        task_id = client.submit_file(
            filename="report.pdf",
            content=b"pdf-content",
            model_version="vlm",
            options={"language": "ch", "is_ocr": True, "page_ranges": "2-4"},
        )
        result = client.get_task_result(task_id)
        assert task_id == "private-task-1"
        assert result.state == "done"
        assert result.full_zip_url == task_id
        assert client.download_result(task_id) == _result_zip()
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/tasks",
        "/tasks/private-task-1",
        "/tasks/private-task-1/result",
    ]
    assert b'name="response_format_zip"' in requests[0].content
    assert b'name="start_page_id"' in requests[0].content
    assert b"1" in requests[0].content
    assert "authorization" not in requests[0].headers


def test_self_hosted_mineru_rejects_non_contiguous_page_ranges() -> None:
    client = SelfHostedMineruClient(
        "http://mineru.internal:8000",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ),
    )
    try:
        with pytest.raises(MineruError, match="仅支持连续页码范围"):
            client.submit_file(
                filename="report.pdf",
                content=b"pdf-content",
                model_version="vlm",
                options={"page_ranges": "2,4-6"},
            )
    finally:
        client.close()


def test_pdf_uses_self_hosted_mineru_task_and_zip_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        ingestion_module, "SelfHostedMineruClient", FakeSelfHostedMineruClient
    )
    monkeypatch.setattr(
        model_config_service.ModelConfigService,
        "build_llm",
        lambda _self: FakeSummaryLLM(),
    )
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client, api_mode="self_hosted")
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "私有解析"}
    ).json()["data"]["id"]

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("private.pdf", _pdf_bytes())},
    )
    assert response.status_code == 200
    document_id = response.json()["data"]["document_id"]
    for _ in range(200):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        ).json()["data"]
        if document["status"] == "ready":
            break
        time.sleep(0.01)
    assert document["status"] == "ready"
    assert document["latest_task"]["api_mode"] == "self_hosted"
    assert document["latest_task"]["task_id"] == "private-task-1"


def test_self_hosted_task_is_resubmitted_after_upstream_restart(
    tmp_path: Path, monkeypatch
) -> None:
    RestartingSelfHostedMineruClient.submit_count = 0
    RestartingSelfHostedMineruClient.query_count = 0
    monkeypatch.setattr(
        ingestion_module, "SelfHostedMineruClient", RestartingSelfHostedMineruClient
    )
    monkeypatch.setattr(
        model_config_service.ModelConfigService,
        "build_llm",
        lambda _self: FakeSummaryLLM(),
    )
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client, api_mode="self_hosted")
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "私有重试"}
    ).json()["data"]["id"]

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("private.pdf", _pdf_bytes())},
    )
    document_id = response.json()["data"]["document_id"]
    for _ in range(200):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        ).json()["data"]
        if document["status"] == "ready":
            break
        time.sleep(0.01)
    assert document["status"] == "ready"
    assert RestartingSelfHostedMineruClient.submit_count == 2


def test_markdown_documents_are_visible_and_pdf_uses_mineru_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ingestion_module, "MineruClient", FakeMineruClient)
    monkeypatch.setattr(
        model_config_service.ModelConfigService,
        "build_llm",
        lambda _self: FakeSummaryLLM(),
    )
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "文档测试"}
    ).json()["data"]["id"]

    markdown = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={
            "file": (
                "notes.md",
                ("# Markdown\n\n" + "这是很长的内容。 " * 250).encode("utf-8"),
            )
        },
    )
    assert markdown.status_code == 200
    markdown_id = markdown.json()["data"]["document_id"]
    for _ in range(100):
        markdown_document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{markdown_id}"
        ).json()["data"]
        if markdown_document["status"] == "ready":
            break
        time.sleep(0.01)
    assert markdown_document["status"] == "ready"
    markdown_artifact = json.loads(
        (
            Path(markdown.json()["data"]["workspace_dir"]) / f"{markdown_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert markdown_artifact["doc_description"] == "文档描述"
    assert markdown_artifact["structure"][0]["summary"] == "节点摘要"

    pdf = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("report.pdf", _pdf_bytes())},
    )
    assert pdf.status_code == 200
    pdf_id = pdf.json()["data"]["document_id"]
    workspace_dir = Path(pdf.json()["data"]["workspace_dir"])

    documents: list[dict[str, object]] = []
    for _ in range(50):
        documents = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents"
        ).json()["data"]
        if any(
            item["id"] == pdf_id and item["status"] == "ready" for item in documents
        ):
            break
        time.sleep(0.01)

    assert {item["id"] for item in documents} >= {markdown_id, pdf_id}
    pdf_record = next(item for item in documents if item["id"] == pdf_id)
    assert pdf_record["parser"] == "mineru"
    assert pdf_record["status"] == "ready"
    artifacts = pdf_record["artifacts"]
    assert isinstance(artifacts, list)
    assert any(
        isinstance(item, dict) and item.get("kind") == "full_markdown"
        for item in artifacts
    )
    workspace_document = json.loads(
        (workspace_dir / f"{pdf_id}.json").read_text(encoding="utf-8")
    )
    assert workspace_document["heading_recovery"]["mode"] == "rules_then_llm"

    content = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{pdf_id}/content"
    )
    assert content.status_code == 200
    assert "MinerU 解析后的完整内容" in content.text


def test_pdf_marks_task_done_only_after_locked_persistence(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ingestion_module, "MineruClient", FakeMineruClient)
    app = create_app(_settings(tmp_path))
    service = app.state.services.documents
    real_lock = ingestion_module.workspace_lock
    lock_state = {"held": False, "manifest_write_locked": False}

    @contextmanager
    def tracked_lock(workspace: Path):
        with real_lock(workspace):
            lock_state["held"] = True
            try:
                yield
            finally:
                lock_state["held"] = False

    real_write_document = service.artifacts.write_document

    def tracked_write_document(*args: object, **kwargs: object):
        lock_state["manifest_write_locked"] = lock_state["held"]
        return real_write_document(*args, **kwargs)

    real_persist_result = service._persist_result

    def tracked_persist_result(
        task: dict[str, object], result_url: str, client: object
    ):
        persisted = service.repository.get_parse_task(str(task["id"]))
        assert persisted is not None
        assert persisted["state"] != "done"
        result = real_persist_result(task, result_url, client)
        persisted = service.repository.get_parse_task(str(task["id"]))
        assert persisted is not None
        assert persisted["state"] != "done"
        return result

    monkeypatch.setattr(ingestion_module, "workspace_lock", tracked_lock)
    monkeypatch.setattr(service.artifacts, "write_document", tracked_write_document)
    monkeypatch.setattr(service, "_persist_result", tracked_persist_result)

    client = TestClient(app)
    _configure_parser(client)
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "持久化顺序", "summary_enabled": False},
    ).json()["data"]["id"]
    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("report.pdf", _pdf_bytes())},
    )
    document_id = response.json()["data"]["document_id"]

    for _ in range(50):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        ).json()["data"]
        if document["status"] == "ready" and document["latest_task"]["state"] == "done":
            break
        time.sleep(0.01)

    assert document["status"] == "ready"
    assert document["latest_task"]["state"] == "done"
    assert lock_state["manifest_write_locked"] is False


def test_batch_upload_keeps_successful_files_when_one_file_is_invalid(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "批量文档", "summary_enabled": False},
    ).json()["data"]

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents/batch",
        files=[
            ("files", ("one.md", b"# One\n\nfirst document")),
            ("files", ("unsupported.txt", b"not supported")),
            ("files", ("two.md", b"# Two\n\nsecond document")),
        ],
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert 0 <= payload["knowledge_base"]["document_count"] <= 2
    assert [(item["filename"], item["ok"]) for item in payload["files"]] == [
        ("one.md", True),
        ("unsupported.txt", False),
        ("two.md", True),
    ]
    assert payload["files"][1]["status_code"] == 415
    for _ in range(100):
        refreshed = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}"
        ).json()["data"]
        if refreshed["document_count"] == 2:
            break
        time.sleep(0.01)
    assert refreshed["document_count"] == 2
    assert (
        len(
            client.get(
                f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents"
            ).json()["data"]
        )
        == 2
    )


def test_pdf_without_summary_does_not_require_llm(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ingestion_module, "MineruClient", FakeMineruClient)
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "纯结构 PDF", "summary_enabled": False},
    ).json()["data"]

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents",
        files={"file": ("report.pdf", _pdf_bytes())},
    )
    assert response.status_code == 200
    document_id = response.json()["data"]["document_id"]
    documents: list[dict[str, object]] = []
    for _ in range(50):
        documents = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents"
        ).json()["data"]
        if any(
            item["id"] == document_id and item["status"] == "ready"
            for item in documents
        ):
            break
        time.sleep(0.01)
    assert any(
        item["id"] == document_id and item["status"] == "ready" for item in documents
    )

    artifact = json.loads(
        (Path(knowledge_base["workspace_dir"]) / f"{document_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert artifact["doc_description"] == ""
    assert "summary" not in artifact["structure"][0]
    assert artifact["heading_recovery"]["mode"] == "fallback"
    assert artifact["structure"][0]["nodes"][0]["nodes"][0]["title"] == "1.1 盈利预测"


def test_missing_mineru_markdown_cleans_claim_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ingestion_module, "MineruClient", MissingMarkdownMineruClient)
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "缺失解析主文件", "summary_enabled": False},
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents",
        files={"file": ("report.pdf", _pdf_bytes())},
    )
    document_id = response.json()["data"]["document_id"]

    for _ in range(100):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents/{document_id}"
        ).json()["data"]
        if document["status"] == "failed":
            break
        time.sleep(0.01)

    assert document["status"] == "failed"
    results = (
        Path(knowledge_base["workspace_dir"])
        / f"staging/{document_id}/g1/parse/results/0000"
    )
    assert not results.exists() or list(results.iterdir()) == []


def test_mineru_resource_is_copied_and_rewritten_during_normalize(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ingestion_module, "MineruClient", ResourceMineruClient)
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "资源归一化", "summary_enabled": False},
    ).json()["data"]
    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents",
        files={"file": ("report.pdf", _pdf_bytes())},
    )
    document_id = response.json()["data"]["document_id"]

    for _ in range(100):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents/{document_id}"
        ).json()["data"]
        if document["status"] == "ready":
            break
        time.sleep(0.01)

    assert document["status"] == "ready"
    normalized_root = (
        Path(knowledge_base["workspace_dir"]) / f"artifacts/{document_id}/g1/normalized"
    )
    assert "![](chunks/0000/assets/result/images/chart.png)" in (
        normalized_root / "full.md"
    ).read_text(encoding="utf-8")
    assert (
        normalized_root / "chunks/0000/assets/result/images/chart.png"
    ).read_bytes() == b"chart"
    page_map = json.loads((normalized_root / "page_map.json").read_text("utf-8"))
    assert page_map["precision"] == "exact"
    assert page_map["entries"][0]["source_page_start"] == 1
    assert page_map["entries"][0]["source_page_end"] == 1
    assert any(
        artifact["kind"] == "asset"
        and artifact["relpath"].endswith(
            "/normalized/chunks/0000/assets/result/images/chart.png"
        )
        for artifact in document["artifacts"]
    )


def test_pdf_heading_recovery_can_be_disabled_for_experiments(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ingestion_module, "MineruClient", FakeMineruClient)
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={
            "name": "关闭标题修复的对照组",
            "summary_enabled": False,
            "heading_recovery_enabled": False,
        },
    ).json()["data"]

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents",
        files={"file": ("report.pdf", _pdf_bytes())},
    )
    assert response.status_code == 200
    document_id = response.json()["data"]["document_id"]
    for _ in range(50):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents/{document_id}"
        ).json()["data"]
        if document["status"] == "ready":
            break
        time.sleep(0.01)
    assert document["status"] == "ready"

    artifact = json.loads(
        (Path(knowledge_base["workspace_dir"]) / f"{document_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert "heading_recovery" not in artifact


def test_pdf_heading_recovery_reuses_summary_llm(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ingestion_module, "MineruClient", FakeMineruClient)

    class TrackingSummaryLLM(FakeSummaryLLM):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def ainvoke(self, prompt: str, **kwargs: object) -> str:
            self.prompts.append(prompt)
            return await super().ainvoke(prompt, **kwargs)

    llm = TrackingSummaryLLM()
    monkeypatch.setattr(
        model_config_service.ModelConfigService,
        "build_llm",
        lambda _self: llm,
    )
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "使用摘要模型的 PDF", "summary_enabled": True},
    ).json()["data"]

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents",
        files={"file": ("report.pdf", _pdf_bytes())},
    )
    assert response.status_code == 200
    document_id = response.json()["data"]["document_id"]
    for _ in range(50):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents/{document_id}"
        ).json()["data"]
        if document["status"] == "ready":
            break
        time.sleep(0.01)
    assert document["status"] == "ready"
    assert any(
        prompt.startswith("You assign hierarchical levels") for prompt in llm.prompts
    )
    assert any("Document Structure:" in prompt for prompt in llm.prompts)


def test_pdf_upload_returns_before_mineru_submission_finishes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ingestion_module, "MineruClient", SlowSubmissionMineruClient)
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "异步提交"}
    ).json()["data"]["id"]

    started = time.monotonic()
    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("report.pdf", _pdf_bytes())},
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 0.2
    document_id = response.json()["data"]["document_id"]
    documents = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents"
    ).json()["data"]
    assert any(
        item["id"] == document_id and item["status"] == "parsing" for item in documents
    )


def test_401_page_pdf_is_chunked_and_published_as_one_document(
    tmp_path: Path, monkeypatch
) -> None:
    ChunkTrackingMineruClient.data_ids = []
    monkeypatch.setattr(ingestion_module, "MineruClient", ChunkTrackingMineruClient)
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "超长 PDF", "summary_enabled": False},
    ).json()["data"]["id"]

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("long.pdf", _pdf_bytes(401))},
    )
    assert response.status_code == 200
    document_id = response.json()["data"]["document_id"]
    for _ in range(400):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        ).json()["data"]
        if document["status"] == "ready":
            break
        time.sleep(0.01)

    assert document["status"] == "ready"
    assert document["parse"] == {
        "chunks_completed": 3,
        "chunks_total": 3,
        "pages_completed": 401,
        "pages_total": 401,
    }
    assert sorted(ChunkTrackingMineruClient.data_ids) == [
        f"{document_id}:g1:c0000",
        f"{document_id}:g1:c0001",
        f"{document_id}:g1:c0002",
    ]
    pipeline = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}/pipeline"
    ).json()["data"]
    assert [
        (task["source_page_start"], task["source_page_end"])
        for task in pipeline["parse_tasks"]
    ] == [(1, 180), (181, 360), (361, 401)]


def test_legacy_unpartitioned_pdf_retry_starts_new_chunked_generation(
    tmp_path: Path, monkeypatch
) -> None:
    ChunkTrackingMineruClient.data_ids = []
    monkeypatch.setattr(ingestion_module, "MineruClient", ChunkTrackingMineruClient)
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    _configure_parser(client)
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "历史超长 PDF", "summary_enabled": False},
    ).json()["data"]["id"]
    service = app.state.services.documents
    repository = service.repository
    knowledge_base = service.knowledge_bases.require_record(knowledge_base_id)
    document_id = "legacy-long-pdf"
    content = _pdf_bytes(401)
    digest = hashlib.sha256(content).hexdigest()
    source_relpath = f"sources/{document_id}-long.pdf"
    source_path = Path(str(knowledge_base["workspace_dir"])) / source_relpath
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(content)
    now = "2026-09-05T00:00:00+00:00"
    repository.create_document(
        {
            "id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "original_filename": "long.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(content),
            "source_relpath": source_relpath,
            "source_sha256": digest,
            "parser": "mineru",
            "status": "failed",
            "current_stage": "parse",
            "stage_state": "failed",
            "failed_stage": "parse",
            "error_code": "PAGE_LIMIT_EXCEEDED",
            "error_message": "PDF exceeds 200 pages",
            "created_at": now,
            "updated_at": now,
            "completed_at": now,
        }
    )
    repository.put_document_artifact(
        {
            "document_id": document_id,
            "kind": "original",
            "relpath": source_relpath,
            "mime_type": "application/pdf",
            "size_bytes": len(content),
            "sha256": digest,
            "pipeline_generation": 1,
            "created_at": now,
        }
    )
    repository.start_upload(
        knowledge_base_id, "legacy-upload", digest, document_id, now
    )
    repository.mark_upload_files_committed(
        knowledge_base_id,
        "legacy-upload",
        source_relpath,
        f"{document_id}.json",
        digest,
        "",
        now,
    )
    repository.fail_upload(
        knowledge_base_id, "legacy-upload", "PDF exceeds 200 pages", now
    )
    repository.create_parse_task(
        {
            "id": "legacy-task",
            "document_id": document_id,
            "provider": "mineru",
            "api_mode": "saas_precision",
            "attempt": 1,
            "pipeline_generation": 1,
            "chunk_index": 0,
            "chunk_count": 1,
            "input_sha256": digest,
            "data_id": document_id,
            "model_version": "vlm",
            "request_json": "{}",
            "state": "failed",
            "dispatch_state": "failed",
            "error_code": "PAGE_LIMIT_EXCEEDED",
            "error_message": "PDF exceeds 200 pages",
            "created_at": now,
            "updated_at": now,
            "completed_at": now,
        }
    )

    retry = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}/retry"
    )
    assert retry.status_code == 200
    assert retry.json()["data"]["pipeline_generation"] == 2

    document: dict[str, object] = {}
    for _ in range(400):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        ).json()["data"]
        if document["status"] == "ready":
            break
        time.sleep(0.01)

    assert document["status"] == "ready"
    assert document["pipeline_generation"] == 2
    assert document["parse"] == {
        "chunks_completed": 3,
        "chunks_total": 3,
        "pages_completed": 401,
        "pages_total": 401,
    }
    assert sorted(ChunkTrackingMineruClient.data_ids) == [
        f"{document_id}:g2:c0000",
        f"{document_id}:g2:c0001",
        f"{document_id}:g2:c0002",
    ]
    pipeline = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}/pipeline"
    ).json()["data"]
    assert [
        (task["source_page_start"], task["source_page_end"])
        for task in pipeline["parse_tasks"]
    ] == [(1, 180), (181, 360), (361, 401)]
    assert repository.list_parse_tasks(document_id, 1)[0]["state"] == "failed"


def test_empty_password_encrypted_pdf_succeeds_when_preflight_is_retried(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ingestion_module, "MineruClient", FakeMineruClient)
    actual_split_pdf = ingestion_module.split_pdf
    split_attempts = 0

    def reject_like_previous_version(*args, **kwargs):
        nonlocal split_attempts
        split_attempts += 1
        if split_attempts == 1:
            raise PdfChunkError("加密 PDF 无法解析")
        return actual_split_pdf(*args, **kwargs)

    monkeypatch.setattr(ingestion_module, "split_pdf", reject_like_previous_version)
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    _configure_parser(client)
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "空密码 PDF", "summary_enabled": False},
    ).json()["data"]["id"]
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("")
    writer.write(output)

    upload = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("encrypted.pdf", output.getvalue())},
    )
    document_id = upload.json()["data"]["document_id"]
    document: dict[str, object] = {}
    for _ in range(200):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        ).json()["data"]
        if document.get("status") == "failed":
            break
        time.sleep(0.01)
    assert document["error_message"] == "加密 PDF 无法解析"

    retry = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}/retry"
    )
    assert retry.status_code == 200
    for _ in range(200):
        document = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        ).json()["data"]
        if document.get("status") == "ready":
            break
        time.sleep(0.01)

    assert document["status"] == "ready"
    assert document["pipeline_generation"] == 2
    assert split_attempts == 2
    task = app.state.services.documents.repository.list_parse_tasks(document_id, 2)[0]
    knowledge_base = app.state.services.documents.knowledge_bases.require_record(
        knowledge_base_id
    )
    chunk_path = Path(str(knowledge_base["workspace_dir"])) / str(
        task["chunk_source_relpath"]
    )
    assert not PdfReader(chunk_path).is_encrypted


def test_mineru_job_scheduling_is_atomic_and_submission_pool_stays_available(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(_settings(tmp_path))
    service = app.state.services.documents
    poll_started = threading.Event()
    release_poll = threading.Event()
    submission_ran = threading.Event()
    poll_calls = 0
    poll_calls_lock = threading.Lock()

    def blocking_poll(_task_id: str) -> None:
        nonlocal poll_calls
        with poll_calls_lock:
            poll_calls += 1
        poll_started.set()
        release_poll.wait(timeout=2)

    monkeypatch.setattr(service, "_poll_task", blocking_poll)
    with ThreadPoolExecutor(max_workers=8) as callers:
        futures = [callers.submit(service._schedule_poll, "task-1") for _ in range(8)]
        for future in futures:
            future.result()

    assert poll_started.wait(timeout=1)
    service._submission_executor.submit(submission_ran.set)
    assert submission_ran.wait(timeout=1)
    assert poll_calls == 1

    release_poll.set()
    service.shutdown()


def test_recover_repairs_missing_and_prematurely_completed_parse_tasks(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    _configure_parser(client)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases", json={"name": "恢复测试"}
    ).json()["data"]
    service = app.state.services.documents
    now = "2026-08-16T00:00:00+00:00"
    source = Path(knowledge_base["workspace_dir"]) / "sources/recover.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_pdf_bytes())
    service.repository.create_document(
        {
            "id": "recover-doc",
            "knowledge_base_id": knowledge_base["id"],
            "original_filename": "recover.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": 10,
            "source_relpath": "sources/recover.pdf",
            "source_sha256": "recover-hash",
            "parser": "mineru",
            "status": "parsing",
            "created_at": now,
            "updated_at": now,
        }
    )
    submitted: list[str] = []
    polled: list[str] = []
    monkeypatch.setattr(service, "_schedule_preparation", submitted.append)
    monkeypatch.setattr(service, "_schedule_poll", polled.append)

    service.recover()

    task = service.repository.create_parse_task(
        {
            "id": "recover-task",
            "document_id": "recover-doc",
            "attempt": 1,
            "data_id": "recover-doc:g1:c0000",
            "api_mode": "saas_precision",
            "model_version": "vlm",
            "created_at": now,
            "updated_at": now,
            "batch_id": "batch-1",
            "state": "done",
        }
    )
    assert submitted == ["recover-doc"]
    submitted.clear()

    service.recover()

    repaired = service.repository.get_parse_task(str(task["id"]))
    assert repaired is not None
    assert repaired["state"] == "pending"
    assert repaired["dispatch_state"] == "waiting"
    assert submitted == []
    assert polled and set(polled) == {task["id"]}


def test_recover_rebuilds_missing_pdf_chunks_in_current_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ApiServerSettings(
        data_dir=tmp_path / "api",
        workspace_root=tmp_path / "workspaces",
        fts_enabled=False,
        pdf_chunk_max_pages=1,
        mineru_poll_interval_seconds=60,
    )
    app = create_app(settings)
    client = TestClient(app)
    _configure_parser(client)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases", json={"name": "缺失分段恢复"}
    ).json()["data"]
    service = app.state.services.documents
    document_id = "partial-pdf"
    source = Path(knowledge_base["workspace_dir"]) / "sources/partial.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    content = _pdf_bytes(2)
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    now = "2026-08-16T00:00:00+00:00"
    service.repository.create_document(
        {
            "id": document_id,
            "knowledge_base_id": knowledge_base["id"],
            "original_filename": "partial.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(content),
            "source_relpath": "sources/partial.pdf",
            "source_sha256": digest,
            "parser": "mineru",
            "status": "parsing",
            "current_stage": "parse",
            "stage_state": "running",
            "created_at": now,
            "updated_at": now,
        }
    )
    service.repository.create_parse_task(
        {
            "id": "partial-pdf-0",
            "document_id": document_id,
            "attempt": 1,
            "pipeline_generation": 1,
            "chunk_index": 0,
            "chunk_count": 2,
            "source_page_start": 1,
            "source_page_end": 1,
            "data_id": f"{document_id}:g1:c0000",
            "api_mode": "saas_precision",
            "model_version": "vlm",
            "state": "done",
            "created_at": now,
            "updated_at": now,
        }
    )
    scheduled: list[str] = []
    monkeypatch.setattr(service, "_schedule_task_submission", scheduled.append)
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda recovered_id: service._prepare_parse_tasks(
            recovered_id, service.models.parser_runtime_config()
        ),
    )

    service._schedule_existing_task(document_id)

    tasks = service.repository.list_parse_tasks(document_id, 1)
    assert [task["chunk_index"] for task in tasks] == [0, 1]
    assert tasks[1]["chunk_count"] == 2
    assert scheduled == [tasks[1]["id"]]
    service.shutdown()


def test_recover_uses_persisted_pdf_plan_after_chunk_setting_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ApiServerSettings(
        data_dir=tmp_path / "api",
        workspace_root=tmp_path / "workspaces",
        fts_enabled=False,
        pdf_chunk_max_pages=1,
        mineru_poll_interval_seconds=60,
    )
    app = create_app(settings)
    client = TestClient(app)
    _configure_parser(client)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases", json={"name": "固定分段计划"}
    ).json()["data"]
    service = app.state.services.documents
    document_id = "planned-pdf"
    source = Path(knowledge_base["workspace_dir"]) / "sources/planned.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    content = _pdf_bytes(2)
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    now = "2026-08-16T00:00:00+00:00"
    service.repository.create_document(
        {
            "id": document_id,
            "knowledge_base_id": knowledge_base["id"],
            "original_filename": "planned.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(content),
            "source_relpath": "sources/planned.pdf",
            "source_sha256": digest,
            "parser": "mineru",
            "status": "parsing",
            "current_stage": "parse",
            "stage_state": "running",
            "created_at": now,
            "updated_at": now,
        }
    )
    created_tasks = 0
    create_task = service.repository.create_parse_task

    def interrupt_after_first_task(values: dict[str, object]) -> dict[str, object]:
        nonlocal created_tasks
        task = create_task(values)
        created_tasks += 1
        if created_tasks == 1:
            raise SystemExit("simulated process exit")
        return task

    monkeypatch.setattr(service, "_schedule_task_submission", lambda _task_id: None)
    monkeypatch.setattr(service.repository, "create_parse_task", interrupt_after_first_task)

    with pytest.raises(SystemExit, match="simulated process exit"):
        service._prepare_parse_tasks(document_id, service.models.parser_runtime_config())

    partial = service.repository.list_parse_tasks(document_id, 1)
    assert [task["chunk_index"] for task in partial] == [0]
    stored = service.repository.get_document(knowledge_base["id"], document_id)
    assert stored is not None
    assert len(json.loads(str(stored["parse_plan_json"]))["chunks"]) == 2
    assert len(
        service._load_pdf_chunk_plan(
            stored, Path(knowledge_base["workspace_dir"]), 1
        )
        or []
    ) == 2

    monkeypatch.setattr(service.repository, "create_parse_task", create_task)
    service.settings = replace(settings, pdf_chunk_max_pages=2)
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda recovered_id: service._prepare_parse_tasks(
            recovered_id, service.models.parser_runtime_config()
        ),
    )

    service._schedule_existing_task(document_id)

    recovered = service.repository.list_parse_tasks(document_id, 1)
    assert [task["chunk_index"] for task in recovered] == [0, 1]
    assert [
        (task["source_page_start"], task["source_page_end"]) for task in recovered
    ] == [(1, 1), (2, 2)]
    service.shutdown()


def test_recovery_requires_new_generation_for_legacy_partial_plan_config_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ApiServerSettings(
        data_dir=tmp_path / "api",
        workspace_root=tmp_path / "workspaces",
        fts_enabled=False,
        pdf_chunk_max_pages=2,
        mineru_poll_interval_seconds=60,
    )
    app = create_app(settings)
    client = TestClient(app)
    _configure_parser(client)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases", json={"name": "旧计划配置冲突"}
    ).json()["data"]
    service = app.state.services.documents
    document_id = "legacy-partial-pdf"
    source = Path(knowledge_base["workspace_dir"]) / "sources/legacy-partial.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    content = _pdf_bytes(2)
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    now = "2026-08-16T00:00:00+00:00"
    service.repository.create_document(
        {
            "id": document_id,
            "knowledge_base_id": knowledge_base["id"],
            "original_filename": "legacy-partial.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(content),
            "source_relpath": "sources/legacy-partial.pdf",
            "source_sha256": digest,
            "parser": "mineru",
            "status": "parsing",
            "current_stage": "parse",
            "stage_state": "running",
            "created_at": now,
            "updated_at": now,
        }
    )
    service.repository.create_parse_task(
        {
            "id": "legacy-partial-pdf-0",
            "document_id": document_id,
            "attempt": 1,
            "pipeline_generation": 1,
            "chunk_index": 0,
            "chunk_count": 2,
            "source_page_start": 1,
            "source_page_end": 1,
            "data_id": f"{document_id}:g1:c0000",
            "api_mode": "saas_precision",
            "model_version": "vlm",
            "state": "created",
            "created_at": now,
            "updated_at": now,
        }
    )
    service.repository.start_upload(
        knowledge_base["id"], "legacy-partial-upload", digest, document_id, now
    )
    service.repository.mark_upload_files_committed(
        knowledge_base["id"],
        "legacy-partial-upload",
        "sources/legacy-partial.pdf",
        f"{document_id}.json",
        digest,
        "",
        now,
    )
    monkeypatch.setattr(service, "_schedule_task_submission", lambda _task_id: None)
    monkeypatch.setattr(
        service,
        "_schedule_preparation",
        lambda recovered_id: service._prepare_parse_tasks(
            recovered_id, service.models.parser_runtime_config()
        ),
    )

    service._schedule_existing_task(document_id)

    restarted = service.repository.get_document(knowledge_base["id"], document_id)
    assert restarted is not None
    assert restarted["status"] == "parsing"
    assert restarted["pipeline_generation"] == 2
    assert restarted["parse_plan_json"] is not None
    assert service.repository.list_parse_tasks(document_id, 1)[0]["state"] == "canceled"
    regenerated = service.repository.list_parse_tasks(document_id, 2)
    assert [task["chunk_index"] for task in regenerated] == [0]
    assert regenerated[0]["chunk_count"] == 1
    service.shutdown()


def test_result_download_lease_heartbeat_keeps_claim_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ApiServerSettings(
        data_dir=tmp_path / "api",
        workspace_root=tmp_path / "workspaces",
        fts_enabled=False,
        mineru_poll_interval_seconds=60,
        pipeline_lease_seconds=1,
    )
    app = create_app(settings)
    client = TestClient(app)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases", json={"name": "下载租约续期"}
    ).json()["data"]
    service = app.state.services.documents
    document_id = "lease-pdf"
    now = ingestion_module.datetime(2026, 8, 16, tzinfo=ingestion_module.timezone.utc)
    clock = {"value": now}

    def fake_now() -> str:
        return clock["value"].isoformat()

    def fake_after(seconds: float) -> str:
        return (clock["value"] + ingestion_module.timedelta(seconds=seconds)).isoformat()

    monkeypatch.setattr(ingestion_module, "_now", fake_now)
    monkeypatch.setattr(ingestion_module, "_after", fake_after)
    service.repository.create_document(
        {
            "id": document_id,
            "knowledge_base_id": knowledge_base["id"],
            "original_filename": "lease.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1,
            "source_relpath": "sources/lease.pdf",
            "source_sha256": "lease-hash",
            "parser": "mineru",
            "status": "parsing",
            "current_stage": "parse",
            "stage_state": "running",
            "created_at": fake_now(),
            "updated_at": fake_now(),
        }
    )
    created = service.repository.create_parse_task(
        {
            "id": "lease-pdf-task",
            "document_id": document_id,
            "attempt": 1,
            "pipeline_generation": 1,
            "chunk_index": 0,
            "chunk_count": 1,
            "data_id": f"{document_id}:g1:c0000",
            "api_mode": "saas_precision",
            "model_version": "vlm",
            "batch_id": "batch-1",
            "state": "pending",
            # Keep the dispatcher away while this test owns the parse-task lease.
            "next_poll_at": "9999-12-31T23:59:59+00:00",
            "created_at": fake_now(),
            "updated_at": fake_now(),
        }
    )
    task = service.repository.claim_parse_task(
        str(created["id"]),
        owner="test",
        mode="poll",
        now="9999-12-31T23:59:59+00:00",
        lease_expires_at=fake_after(settings.pipeline_lease_seconds),
    )
    assert task is not None
    renewals: list[str] = []
    renew = service.repository.renew_claimed_parse_task_lease

    def track_renewal(*args: object, **kwargs: object) -> bool:
        renewals.append(fake_now())
        return renew(*args, **kwargs)

    class SlowClient:
        downloads = 0

        def download_result_to(
            self,
            _url: str,
            destination: Path,
            *,
            max_bytes: int,
            reserve_bytes: Callable[[int], bool],
        ) -> int:
            type(self).downloads += 1
            payload = _result_zip()
            assert len(payload) <= max_bytes
            for _ in range(5):
                clock["value"] += ingestion_module.timedelta(seconds=0.25)
                time.sleep(0.04)
            assert reserve_bytes(len(payload))
            destination.write_bytes(payload)
            return len(payload)

    monkeypatch.setattr(
        service.repository, "renew_claimed_parse_task_lease", track_renewal
    )
    monkeypatch.setattr(service, "_parse_lease_maintenance_interval", lambda: 0.01)

    persisted = service._persist_result(task, "https://result.invalid/file.zip", SlowClient())

    assert SlowClient.downloads == 1
    assert len(renewals) > 1
    assert service.repository.update_claimed_parse_task(
        str(task["id"]),
        str(task["lease_token"]),
        {
            "state": "done",
            "dispatch_state": "succeeded",
            "result_root_relpath": persisted["result_root_relpath"],
            "output_sha256": persisted["output_sha256"],
            "updated_at": fake_now(),
        },
        release=True,
    )
    service.shutdown()


def test_recover_requeues_normalization_after_completed_parse_handoff_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ingestion_module.DocumentIngestionService)

    class Repository:
        def list_parse_tasks(self, _document_id: str) -> list[dict[str, object]]:
            return [
                {
                    "id": "parse-1",
                    "state": "done",
                    "pipeline_generation": 3,
                }
            ]

    service.repository = Repository()  # type: ignore[assignment]
    monkeypatch.setattr(
        service,
        "_find_document",
        lambda _document_id: {"pipeline_generation": 3},
    )
    queued: list[tuple[str, int]] = []
    monkeypatch.setattr(
        service,
        "_queue_normalization_if_complete",
        lambda document_id, generation: queued.append((document_id, generation)),
    )

    service._schedule_existing_task("doc-1")

    assert queued == [("doc-1", 3)]


def test_markdown_recovery_requeues_missing_initial_normalization_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ingestion_module.DocumentIngestionService)

    class Repository:
        def list_unqueued_markdown_normalization_documents(
            self,
        ) -> list[dict[str, object]]:
            return [
                {
                    "id": "doc-1",
                    "pipeline_generation": 1,
                    "source_relpath": "sources/doc-1.md",
                    "source_sha256": "source-hash",
                }
            ]

        def list_parsing_documents(self) -> list[dict[str, object]]:
            return []

    class Pipeline:
        def recover(self) -> None:
            return None

    service.repository = Repository()  # type: ignore[assignment]
    service.pipeline = Pipeline()  # type: ignore[assignment]
    service._dispatcher_wake = threading.Event()
    queued: list[str] = []
    monkeypatch.setattr(
        service,
        "_queue_markdown_normalization",
        lambda document, _now: queued.append(str(document["id"])),
    )

    service.recover()

    assert queued == ["doc-1"]


def test_markdown_requeue_skips_already_published_document() -> None:
    service = object.__new__(ingestion_module.DocumentIngestionService)

    class Repository:
        def get_latest_normalization_task(self, *_args: object) -> None:
            raise AssertionError("已发布文档不应查询或创建 normalize 任务")

    service.repository = Repository()  # type: ignore[assignment]

    service._queue_markdown_normalization(
        {
            "id": "doc-1",
            "pipeline_generation": 1,
            "parser": "native_markdown",
            "status": "ready",
            "current_stage": "complete",
            "stage_state": "succeeded",
            "parsed_content_version": 4,
            "source_relpath": "sources/doc.md",
            "source_sha256": "hash",
        },
        "2026-09-10T00:00:00+00:00",
    )


def test_workspace_recovery_materializes_markdown_before_index_recovery(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    knowledge_base = client.post(
        "/api/v1/knowledge-bases", json={"name": "Markdown 恢复"}
    ).json()["data"]
    workspace = Path(knowledge_base["workspace_dir"])
    document_id = "recovered-markdown"
    source_relpath = f"sources/{document_id}.md"
    source = workspace / source_relpath
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# 恢复文档\n\n正文", encoding="utf-8")
    (workspace / f"{document_id}.json").write_text(
        json.dumps(
            {
                "id": document_id,
                "type": "markdown",
                "doc_name": "恢复文档.md",
                "doc_description": "",
                "path": source_relpath,
                "line_count": 3,
                "structure": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (workspace / "_meta.json").write_text(
        json.dumps(
            {
                document_id: {
                    "type": "markdown",
                    "doc_name": "恢复文档.md",
                    "doc_description": "",
                    "path": source_relpath,
                    "line_count": 3,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = app.state.services.documents
    assert service.repository.get_document(knowledge_base["id"], document_id) is None

    service.recover_workspace_documents()

    document = service.repository.get_document(knowledge_base["id"], document_id)
    assert document is not None
    assert document["status"] == "ready"
    assert document["parsed_markdown_relpath"] == source_relpath
    assert document["fts_indexed_version"] is None


def test_concurrent_same_markdown_upload_creates_one_document(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "并发去重", "summary_enabled": False},
    ).json()["data"]["id"]
    service = app.state.services.documents

    def upload() -> object:
        return service.upload(knowledge_base_id, "same.md", b"# Same\n\ncontent")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(upload) for _ in range(2)]
        responses = [future.result() for future in futures]

    document_ids = {response.document_id for response in responses}
    replay_flags = sorted(bool(response.idempotent_replay) for response in responses)
    documents = service.repository.list_documents(knowledge_base_id)
    assert len(document_ids) == 1
    assert replay_flags == [False, True]
    assert [document["id"] for document in documents] == list(document_ids)


def test_failed_pdf_can_be_retried_without_uploading_again(
    tmp_path: Path, monkeypatch
) -> None:
    RetryMineruClient.submit_count = 0
    RetryMineruClient.poll_count = 0
    RetryMineruClient.submissions_by_data_id = {}
    monkeypatch.setattr(ingestion_module, "MineruClient", RetryMineruClient)
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "解析重试", "summary_enabled": False}
    ).json()["data"]["id"]

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("report.pdf", _pdf_bytes())},
    )
    assert response.status_code == 200
    document_id = response.json()["data"]["document_id"]
    failed: dict[str, object] | None = None
    for _ in range(200):
        documents = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents"
        ).json()["data"]
        failed = next(
            (
                item
                for item in documents
                if item["id"] == document_id and item["status"] == "failed"
            ),
            None,
        )
        if failed is not None:
            break
        time.sleep(0.01)
    assert failed is not None

    retry = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}/retry"
    )
    assert retry.status_code == 200
    assert retry.json()["data"]["status"] == "parsing"
    assert retry.json()["data"]["latest_task"]["attempt"] == 2

    ready: dict[str, object] | None = None
    for _ in range(200):
        documents = client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents"
        ).json()["data"]
        ready = next(
            (
                item
                for item in documents
                if item["id"] == document_id and item["status"] == "ready"
            ),
            None,
        )
        if ready is not None:
            break
        time.sleep(0.01)
    assert ready is not None
    assert RetryMineruClient.submit_count == 2
