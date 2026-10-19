"""The supported text-only subset of OpenAI Chat Completions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str = Field(max_length=32000)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[TextPart]

    def text(self) -> str:
        return (
            self.content
            if isinstance(self.content, str)
            else "".join(part.text for part in self.content)
        )


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=128)
    stream: bool = False
    stream_options: StreamOptions | None = None
    user: str | None = Field(default=None, min_length=1, max_length=128)
    n: Literal[1] = 1

    @model_validator(mode="after")
    def validate_text_conversation(self) -> "ChatCompletionRequest":
        if not self.model.strip() or (self.user is not None and not self.user.strip()):
            raise ValueError("model 和 user 不能是空白")
        if self.messages[-1].role != "user":
            raise ValueError("最后一条消息必须是 user")
        if any(
            not item.text().strip() or len(item.text()) > 32000
            for item in self.messages
        ):
            raise ValueError("每条消息必须包含 1 到 32000 字符的文本")
        if sum(len(item.text()) for item in self.messages) > 128000:
            raise ValueError("messages 文本总长不能超过 128000 字符")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options 仅适用于流式调用")
        return self

    def runtime_messages(self) -> list[dict[str, str]]:
        return [
            {
                "role": "system" if item.role == "developer" else item.role,
                "content": item.text(),
            }
            for item in self.messages
        ]


class CompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class CompletionChoice(BaseModel):
    index: Literal[0] = 0
    message: AssistantMessage
    finish_reason: Literal["stop"] = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[CompletionChoice]
    usage: CompletionUsage | None = None


class CompletionDelta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None


class ChunkChoice(BaseModel):
    index: Literal[0] = 0
    delta: CompletionDelta
    finish_reason: Literal["stop"] | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
    usage: CompletionUsage | None = None


class ModelResponse(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: Literal["nianlun"] = "nianlun"


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelResponse]
