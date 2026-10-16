"""Strict contracts for application-key authenticated public execution."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PublicChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=32000)
    user_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    response_mode: Literal["blocking", "streaming"] = "blocking"

    @field_validator("message", "user_id")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class PublicSnippet(BaseModel):
    model_config = ConfigDict(extra="ignore")
    citation_id: int | None = None
    doc_id: str
    doc_name: str | None = None
    node_id: str | None = None
    line_spec: str | None = None
    line_num: int | None = None
    title: str | None = None
    text: str = ""
    char_offset: int | None = None
    char_limit: int | None = None
    total_chars: int | None = None
    text_truncated: bool = False
    content_version: int | None = None


class PublicClarification(BaseModel):
    """Whitelisted clarification projection; internal protocol fields stay private."""

    model_config = ConfigDict(extra="forbid")
    clarification_id: str
    clarification_type: str
    status: str
    question: str
    context: str | None = None
    options: list[str] = Field(default_factory=list)
    duplicate: bool = False


class PublicChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str
    release_id: str
    run_id: str
    conversation_id: str
    message_id: str
    answer: str
    retrieved_snippets: list[PublicSnippet] = Field(default_factory=list)
    usage: dict[str, int] | None = None
    ttft_ms: int | None = None
    clarification: PublicClarification | None = None
    knowledge_base_content_version: int


class PublicRunResponse(BaseModel):
    run_id: str
    app_id: str
    client_id: str
    release_id: str
    conversation_id: str
    status: str
    error_type: str | None = None
    error_status: int | None = None
    error_message: str | None = None
    result: PublicChatResponse | None = None
    created_at: str
    updated_at: str
    knowledge_base_content_version: int
    request_id: str | None = None
    duration_ms: int
    usage: dict[str, int] | None = None
