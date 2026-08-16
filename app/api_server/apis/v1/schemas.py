"""Pydantic contracts shared by version-one endpoints."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApiSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


def _normalize_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url 必须是 http 或 https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url 不能包含用户名或密码")
    return normalized.rstrip("/")


class ModelEndpointConfigUpdate(ApiSchema):
    """Common endpoint fields; API keys are accepted only on write."""

    base_url: str | None = Field(default=None, max_length=2_048)
    api_key: str | None = Field(default=None, max_length=4_096, repr=False)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str | None) -> str | None:
        return _normalize_base_url(value)

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None


class ChatRequest(ApiSchema):
    message: str = Field(..., min_length=1, max_length=32_000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    response_mode: Literal["blocking", "streaming"] = "blocking"
    user_id: str = Field(default="anonymous", min_length=1, max_length=128)
    clarification_enabled: bool = True


class ToolCall(ApiSchema):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int | None = None
    tool_call_id: str | None = None
    batch: int | None = None


class ChatResponse(ApiSchema):
    app_id: str
    conversation_id: str
    message_id: str
    answer: str
    route: str
    retrieved_snippets: list[dict[str, Any]]
    status_events: list[dict[str, Any]]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int] | None = None
    ttft_ms: int | None = None
    clarification: dict[str, Any] | None = None


class ConversationResponse(ApiSchema):
    id: str
    application_id: str
    title: str
    status: Literal["active", "archived", "deleted"]
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = None
    message_count: int = 0


class MessageSourceResponse(ApiSchema):
    id: str
    message_id: str
    source_order: int
    citation_id: int
    doc_id: str
    doc_name: str | None = None
    node_id: str | None = None
    line_spec: str | None = None
    line_num: int | None = None
    title: str | None = None
    text: str
    char_offset: int | None = None
    char_limit: int | None = None
    total_chars: int | None = None
    text_truncated: bool = False
    content_version: int | None = None


class ConversationMessageResponse(ApiSchema):
    id: str
    conversation_id: str
    seq_no: int
    role: Literal["user", "assistant"]
    content: str
    status: Literal["pending", "completed", "failed"]
    route: str | None = None
    error_message: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int] | None = None
    ttft_ms: int | None = None
    created_at: datetime
    updated_at: datetime
    sources: list[MessageSourceResponse] = Field(default_factory=list)


class KnowledgeBaseCreateRequest(ApiSchema):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="", max_length=2_000)
    summary_enabled: bool = True
    embedding_model_id: str | None = Field(default=None, max_length=128)

    @field_validator("embedding_model_id")
    @classmethod
    def normalize_embedding_model_id(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None


class KnowledgeBaseUpdateRequest(ApiSchema):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    summary_enabled: bool | None = None
    embedding_model_id: str | None = Field(default=None, max_length=128)
    vector_enabled: bool | None = None

    @field_validator("embedding_model_id")
    @classmethod
    def normalize_embedding_model_id(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("知识库名称不能为空")
        return normalized


class ModelConfigTestRequest(ModelEndpointConfigUpdate):
    """Optional current-form values used for a model connectivity probe."""

    target: Literal["llm", "embedding", "parser"]
    model: str | None = Field(default=None, max_length=256)
    dimension: int | None = Field(default=None, gt=0, le=100_000)
    api_mode: Literal["saas_precision", "self_hosted"] = "saas_precision"

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None


class ModelConfigTestResponse(ApiSchema):
    target: Literal["llm", "embedding", "parser"]
    ok: bool
    message: str


class ModelProfileRequest(ModelEndpointConfigUpdate):
    """Create or fully update one model catalog entry."""

    kind: Literal["llm", "embedding", "parser"]
    name: str = Field(..., min_length=1, max_length=128)
    base_url: str | None = Field(default=..., max_length=2_048)
    model: str | None = Field(default=None, max_length=256)
    context_window_tokens: int | None = Field(default=None, gt=0, le=10_000_000)
    dimension: int | None = Field(default=None, gt=0, le=100_000)
    # Kept for backwards-compatible clients; knowledge-base selection is the
    # source of truth for whether an Embedding profile is used.
    enabled: bool = True
    api_mode: Literal["saas_precision", "self_hosted"] = "saas_precision"
    model_version: Literal["pipeline", "vlm"] = "vlm"
    language: str = Field(default="ch", min_length=1, max_length=64)
    is_ocr: bool = False
    enable_table: bool = True
    enable_formula: bool = True
    page_ranges: str = Field(default="", max_length=256)
    is_default: bool = False

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("模型名称不能为空")
        return normalized

    @field_validator("base_url")
    @classmethod
    def require_profile_base_url(cls, value: str | None) -> str:
        normalized = _normalize_base_url(value)
        if normalized is None:
            raise ValueError("模型 API URL 不能为空")
        return normalized

    @field_validator("model")
    @classmethod
    def normalize_profile_model(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None

    @model_validator(mode="after")
    def validate_catalog_model(self) -> "ModelProfileRequest":
        if self.kind in {"llm", "embedding"} and not self.model:
            raise ValueError(f"{self.kind} 模型名称不能为空")
        if self.kind != "llm" and self.context_window_tokens is not None:
            raise ValueError("context_window_tokens 仅适用于 LLM")
        return self

    @field_validator("language")
    @classmethod
    def normalize_profile_language(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or not re.fullmatch(r"[A-Za-z0-9_]+", normalized):
            raise ValueError("language 必须是 MinerU 语言标识")
        return normalized

    @field_validator("page_ranges")
    @classmethod
    def normalize_profile_page_ranges(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        ranges: list[str] = []
        for part in normalized.split(","):
            item = part.strip()
            match = re.fullmatch(r"([1-9][0-9]*)(?:-([1-9][0-9]*))?", item)
            if match is None:
                raise ValueError("page_ranges 必须是 1-20,22 形式")
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if start > end:
                raise ValueError("page_ranges 范围起始页不能大于结束页")
            ranges.append(str(start) if start == end else f"{start}-{end}")
        return ",".join(ranges)


class ModelProfileResponse(ApiSchema):
    id: str
    kind: Literal["llm", "embedding", "parser"]
    name: str
    model: str | None
    context_window_tokens: int | None
    base_url: str | None
    api_key_configured: bool
    enabled: bool
    api_mode: Literal["saas_precision", "self_hosted"]
    dimension: int | None
    model_version: Literal["pipeline", "vlm"]
    language: str
    is_ocr: bool
    enable_table: bool
    enable_formula: bool
    page_ranges: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseResponse(ApiSchema):
    id: str
    name: str
    description: str
    status: Literal["creating", "ready", "indexing", "error"]
    document_count: int
    summary_enabled: bool = True
    workspace_dir: str
    workspace_relpath: str | None = None
    content_version: int = 0
    fts_status: Literal["disabled", "pending", "building", "ready", "failed"] = (
        "disabled"
    )
    fts_revision: int | None = None
    fts_target_revision: int | None = None
    fts_collection: str | None = None
    fts_error: str | None = None
    vector_status: Literal["disabled", "pending", "building", "ready", "failed"] = (
        "disabled"
    )
    vector_enabled: bool = False
    vector_revision: int | None = None
    vector_target_revision: int | None = None
    vector_collection: str | None = None
    vector_error: str | None = None
    # This is the model selected for this knowledge base. The vector_* fields
    # below also keep the fingerprint of the last completed build.
    embedding_model_id: str | None = None
    vector_model_id: str | None = None
    vector_model_updated_at: str | None = None
    vector_dimension: int | None = None
    vector_progress_stage: str | None = None
    vector_documents_total: int | None = None
    vector_documents_completed: int | None = None
    vector_records_processed: int | None = None
    document_id: str | None = None
    idempotent_replay: bool = False
    created_at: datetime
    updated_at: datetime


class DocumentArtifactResponse(ApiSchema):
    id: str
    document_id: str
    kind: Literal[
        "original",
        "result_zip",
        "full_markdown",
        "content_list",
        "layout",
        "model",
        "asset",
    ]
    relpath: str
    mime_type: str
    size_bytes: int
    created_at: datetime


class DocumentParseTaskResponse(ApiSchema):
    id: str
    document_id: str
    provider: Literal["mineru"]
    api_mode: Literal["saas_precision", "self_hosted"]
    attempt: int
    data_id: str
    batch_id: str | None
    task_id: str | None
    model_version: Literal["pipeline", "vlm"]
    state: Literal[
        "created",
        "uploading",
        "waiting-file",
        "pending",
        "running",
        "converting",
        "done",
        "failed",
    ]
    extracted_pages: int | None = None
    total_pages: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class DocumentResponse(ApiSchema):
    id: str
    knowledge_base_id: str
    original_filename: str
    file_extension: str
    mime_type: str
    size_bytes: int
    parser: Literal["native_markdown", "mineru"]
    status: Literal[
        "uploaded", "parsing", "parsed", "indexing", "ready", "failed", "deleted"
    ]
    parsed_content_version: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    latest_task: DocumentParseTaskResponse | None = None
    artifacts: list[DocumentArtifactResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class ApplicationCreateRequest(ApiSchema):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="", max_length=2_000)
    knowledge_base_id: str
    llm_model_id: str | None = Field(default=None, max_length=128)
    provider: str = Field(default="default", min_length=1, max_length=128)

    @field_validator("llm_model_id")
    @classmethod
    def normalize_llm_model_id(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None


class ApplicationResponse(ApiSchema):
    id: str
    name: str
    description: str
    knowledge_base_id: str
    llm_model_id: str | None
    provider: str
    created_at: datetime
    updated_at: datetime


__all__ = [
    "ApplicationCreateRequest",
    "ApplicationResponse",
    "ApiSchema",
    "ChatRequest",
    "ChatResponse",
    "ConversationMessageResponse",
    "ConversationResponse",
    "DocumentArtifactResponse",
    "DocumentParseTaskResponse",
    "DocumentResponse",
    "KnowledgeBaseCreateRequest",
    "KnowledgeBaseUpdateRequest",
    "KnowledgeBaseResponse",
    "ModelConfigTestRequest",
    "ModelConfigTestResponse",
    "ModelProfileRequest",
    "ModelProfileResponse",
    "MessageSourceResponse",
    "ToolCall",
]
