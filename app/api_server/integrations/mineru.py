"""HTTP adapters for MinerU SaaS Precision API and self-hosted mineru-api."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class MineruError(RuntimeError):
    """An upstream MinerU request failed or returned an invalid payload."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MineruUploadTicket:
    batch_id: str
    upload_url: str


@dataclass(frozen=True)
class MineruBatchResult:
    state: str
    data_id: str | None
    full_zip_url: str | None
    extracted_pages: int | None
    total_pages: int | None
    error_code: str | None
    error_message: str | None


class MineruClient:
    """Use only the official signed-upload Precision API flow."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not base_url.strip():
            raise MineruError("MinerU API URL 未配置")
        if not api_key.strip():
            raise MineruError("MinerU API Key 未配置")
        root = base_url.rstrip("/")
        self.api_root = root if root.endswith("/api/v4") else f"{root}/api/v4"
        self.api_key = api_key
        self._client = http_client or httpx.Client(timeout=timeout)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def response_error(response: httpx.Response, payload: Any = None) -> MineruError:
        code: str | None = f"HTTP_{response.status_code}"
        message = f"MinerU API 返回 HTTP {response.status_code}"
        if isinstance(payload, dict):
            raw_code = payload.get("code")
            if raw_code is not None:
                code = str(raw_code)
            raw_message = payload.get("msg") or payload.get("message")
            if raw_message:
                message = str(raw_message)
        if response.status_code in {401, 403}:
            message = "MinerU API Key 无效或没有访问权限"
        return MineruError(message, code=code)

    def _json_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise MineruError("MinerU API 连接失败，请检查 URL 和网络") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise MineruError("MinerU API 返回了无效 JSON") from exc
        if response.status_code >= 400 or not isinstance(payload, dict):
            raise self.response_error(response, payload)
        if payload.get("code") not in (None, 0, "0"):
            raise self.response_error(response, payload)
        return payload

    def request_upload_url(
        self,
        *,
        filename: str,
        data_id: str,
        model_version: str,
        options: dict[str, Any] | None = None,
    ) -> MineruUploadTicket:
        body: dict[str, Any] = {
            "files": [{"name": filename, "data_id": data_id}],
            "model_version": model_version,
        }
        if options:
            body.update(
                {key: value for key, value in options.items() if value is not None}
            )
        payload = self._json_request(
            "POST",
            f"{self.api_root}/file-urls/batch",
            headers=self._headers(),
            json=body,
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MineruError("MinerU 上传接口缺少 data")
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls")
        if not isinstance(batch_id, str) or not batch_id:
            raise MineruError("MinerU 上传接口缺少 batch_id")
        if (
            not isinstance(file_urls, list)
            or not file_urls
            or not isinstance(file_urls[0], str)
        ):
            raise MineruError("MinerU 上传接口缺少签名地址")
        return MineruUploadTicket(batch_id=batch_id, upload_url=file_urls[0])

    def upload_file(self, upload_url: str, content: bytes) -> None:
        try:
            response = self._client.put(upload_url, content=content)
        except httpx.HTTPError as exc:
            raise MineruError("上传文件到 MinerU 失败") from exc
        if response.status_code >= 400:
            raise MineruError(f"上传文件到 MinerU 返回 HTTP {response.status_code}")

    def get_batch_result(self, batch_id: str) -> MineruBatchResult:
        payload = self._json_request(
            "GET",
            f"{self.api_root}/extract-results/batch/{batch_id}",
            headers=self._headers(),
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MineruError("MinerU 批次接口缺少 data")
        results = data.get("extract_result")
        if not isinstance(results, list) or not results:
            raise MineruError("MinerU 批次接口缺少 extract_result")
        result = results[0]
        if not isinstance(result, dict):
            raise MineruError("MinerU 单文件结果格式无效")
        state = str(result.get("state") or "pending")
        return MineruBatchResult(
            state=state,
            data_id=str(result["data_id"])
            if result.get("data_id") is not None
            else None,
            full_zip_url=(
                str(result["full_zip_url"]) if result.get("full_zip_url") else None
            ),
            extracted_pages=_int_or_none(result.get("extracted_pages")),
            total_pages=_int_or_none(result.get("total_pages")),
            error_code=(str(result["err_code"]) if result.get("err_code") else None),
            error_message=(str(result["err_msg"]) if result.get("err_msg") else None),
        )

    def download_result(self, result_url: str) -> bytes:
        try:
            response = self._client.get(result_url)
        except httpx.HTTPError as exc:
            raise MineruError("下载 MinerU 解析结果失败") from exc
        if response.status_code >= 400:
            raise MineruError(f"下载 MinerU 解析结果返回 HTTP {response.status_code}")
        return response.content


class SelfHostedMineruClient:
    """Adapter for the official self-hosted ``mineru-api`` FastAPI service."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not base_url.strip():
            raise MineruError("MinerU API URL 未配置")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self._client = http_client or httpx.Client(timeout=timeout)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def submit_file(
        self,
        *,
        filename: str,
        content: bytes,
        model_version: str,
        options: dict[str, Any] | None = None,
    ) -> str:
        fields: dict[str, str] = {
            "backend": "vlm-engine" if model_version == "vlm" else "pipeline",
            "return_md": "true",
            "response_format_zip": "true",
        }
        option_mapping = {
            "language": "lang_list",
            "is_ocr": "parse_method",
            "enable_table": "table_enable",
            "enable_formula": "formula_enable",
        }
        for key, value in (options or {}).items():
            if key == "is_ocr":
                fields["parse_method"] = "ocr" if value else "auto"
            elif key == "page_ranges":
                # SaaS page ranges are 1-indexed; mineru-api uses 0-indexed bounds.
                start, end = _self_hosted_page_range_bounds(str(value))
                if start is not None and end is not None:
                    fields["start_page_id"] = str(start - 1)
                    fields["end_page_id"] = str(end - 1)
            elif key in option_mapping and value is not None:
                fields[option_mapping[key]] = str(value).lower()
        try:
            response = self._client.post(
                f"{self.base_url}/tasks",
                headers=self._headers(),
                data=fields,
                files={"files": (filename, content, "application/octet-stream")},
            )
        except httpx.HTTPError as exc:
            raise MineruError("MinerU API 连接失败，请检查 URL 和网络") from exc
        if response.status_code >= 400:
            raise MineruClient.response_error(response, _response_json(response))
        payload = _response_json(response)
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise MineruError("私有 MinerU 提交接口缺少 task_id")
        return task_id

    def get_task_result(self, task_id: str) -> MineruBatchResult:
        try:
            response = self._client.get(
                f"{self.base_url}/tasks/{task_id}", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise MineruError("MinerU API 连接失败，请检查 URL 和网络") from exc
        payload = _response_json(response)
        if response.status_code >= 400:
            raise MineruClient.response_error(response, payload)
        if not isinstance(payload, dict):
            raise MineruError("私有 MinerU 任务接口返回无效 JSON")
        raw_state = str(
            payload.get("status") or payload.get("state") or "pending"
        ).lower()
        state = _self_hosted_state(raw_state)
        error = payload.get("error") or payload.get("message")
        return MineruBatchResult(
            state=state,
            data_id=None,
            full_zip_url=task_id if state == "done" else None,
            extracted_pages=_int_or_none(payload.get("extracted_pages")),
            total_pages=_int_or_none(payload.get("total_pages")),
            error_code=raw_state if state == "failed" else None,
            error_message=str(error) if state == "failed" and error else None,
        )

    def download_result(self, task_id: str) -> bytes:
        try:
            response = self._client.get(
                f"{self.base_url}/tasks/{task_id}/result", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise MineruError("下载 MinerU 解析结果失败") from exc
        if response.status_code >= 400:
            raise MineruClient.response_error(response, _response_json(response))
        return response.content


def _response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _self_hosted_state(value: str) -> str:
    if value in {"done", "completed", "success", "succeeded"}:
        return "done"
    if value in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if value in {"running", "processing"}:
        return "running"
    return "pending"


def _self_hosted_page_range_bounds(value: str) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    if "," in value:
        raise MineruError("私有 MinerU 仅支持连续页码范围，例如 2-6")
    start_text, _, end_text = value.strip().partition("-")
    try:
        start = int(start_text)
        end = int(end_text or start_text)
    except ValueError as exc:
        raise MineruError("私有 MinerU 页码范围格式无效") from exc
    if start < 1 or end < start:
        raise MineruError("私有 MinerU 页码范围无效")
    return start, end


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "MineruBatchResult",
    "MineruClient",
    "MineruError",
    "MineruUploadTicket",
    "SelfHostedMineruClient",
]
