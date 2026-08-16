from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.api_server.services.document_ingestion_service as ingestion_module
import app.api_server.services.model_config_service as model_config_service
from app.api_server.config import ApiServerSettings
from app.api_server.integrations.mineru import (
    MineruBatchResult,
    MineruError,
    MineruUploadTicket,
    SelfHostedMineruClient,
)
from app.api_server.services.documents.mineru_artifacts import (
    extract_result_archive,
    select_markdown_result,
)
from app.api_server.services.documents.mineru_tasks import build_parser_options
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
            "result/full.md", "# PDF 标题\n\n这是 MinerU 解析后的完整内容。"
        )
        archive.writestr("result/content_list.json", '[{"type": "text"}]')
    return output.getvalue()


def _self_hosted_result_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("private.md", "# 私有 PDF 标题\n\n这是私有 MinerU 的结果。")
    return output.getvalue()


def test_result_archive_rejects_path_traversal(tmp_path: Path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../outside.md", "not allowed")

    with pytest.raises(MineruError, match="非法路径"):
        extract_result_archive(
            output.getvalue(), tmp_path / "parsed", max_member_bytes=1024
        )


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

    def request_upload_url(self, **kwargs: object) -> MineruUploadTicket:
        type(self).submit_count += 1
        return MineruUploadTicket(
            f"batch-{self.submit_count}", "https://upload.invalid/file"
        )

    def get_batch_result(self, _batch_id: str) -> MineruBatchResult:
        type(self).poll_count += 1
        if type(self).poll_count == 1:
            return MineruBatchResult(
                "failed", "doc-1", None, 0, 1, "TEMP", "temporary failure"
            )
        return super().get_batch_result(_batch_id)


class FakeSummaryLLM:
    def invoke(self, _prompt: str, **_kwargs: object) -> str:
        return "文档描述"

    async def ainvoke(self, prompt: str, **_kwargs: object) -> str:
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
        files={"file": ("private.pdf", b"pdf-content")},
    )
    assert response.status_code == 200
    document_id = response.json()["data"]["document_id"]
    for _ in range(50):
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
        files={"file": ("private.pdf", b"pdf-content")},
    )
    document_id = response.json()["data"]["document_id"]
    for _ in range(50):
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
    markdown_artifact = json.loads(
        (
            Path(markdown.json()["data"]["workspace_dir"]) / f"{markdown_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert markdown_artifact["doc_description"] == "文档描述"
    assert markdown_artifact["structure"][0]["summary"] == "节点摘要"

    pdf = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("report.pdf", b"%PDF-fake")},
    )
    assert pdf.status_code == 200
    pdf_id = pdf.json()["data"]["document_id"]

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
        files={"file": ("report.pdf", b"%PDF-fake")},
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
    assert lock_state["manifest_write_locked"] is True


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
    assert payload["knowledge_base"]["document_count"] == 2
    assert [(item["filename"], item["ok"]) for item in payload["files"]] == [
        ("one.md", True),
        ("unsupported.txt", False),
        ("two.md", True),
    ]
    assert payload["files"][1]["status_code"] == 415
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
        files={"file": ("report.pdf", b"%PDF-fake")},
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
        files={"file": ("report.pdf", b"%PDF-fake")},
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
    monkeypatch.setattr(service, "_schedule_submission", submitted.append)
    monkeypatch.setattr(service, "_schedule_poll", polled.append)

    service.recover()

    task = service.repository.get_latest_parse_task("recover-doc")
    assert task is not None
    assert task["state"] == "created"
    assert submitted == ["recover-doc"]
    assert polled == []

    service.repository.update_parse_task(
        str(task["id"]),
        {"state": "done", "batch_id": "batch-1", "updated_at": now},
    )
    submitted.clear()
    service.recover()

    assert submitted == []
    assert polled == [task["id"]]


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
    monkeypatch.setattr(ingestion_module, "MineruClient", RetryMineruClient)
    client = TestClient(create_app(_settings(tmp_path)))
    _configure_parser(client)
    knowledge_base_id = client.post(
        "/api/v1/knowledge-bases", json={"name": "解析重试", "summary_enabled": False}
    ).json()["data"]["id"]

    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("report.pdf", b"%PDF-fake")},
    )
    assert response.status_code == 200
    document_id = response.json()["data"]["document_id"]
    failed: dict[str, object] | None = None
    for _ in range(50):
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
    for _ in range(50):
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
