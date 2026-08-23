"""LLM 工厂、结构化输出与 content 归一化的统一入口。

被 ``agent/lead_agent`` 与 ``indexing/tree`` 共同依赖：ChatOpenAI 构造见
:func:`build_chat_model`，统一模型客户端构造见 :func:`build_llm`，消息 / 裸
content 的文本提取见 :func:`content_to_text`。

env 解析复用顶层 ``nianlun.config`` 的 ``get_openai_*`` 取值函数（``.env`` 加载
也在 config 完成），本模块不自行 ``load_dotenv`` / 读 ``os.environ``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, TypeVar, cast

from json_repair import repair_json
from pydantic import BaseModel, ValidationError

from nianlun.config import (
    get_openai_api_key,
    get_openai_base_url,
    get_openai_model,
)

from langchain_openai import ChatOpenAI

StructuredResultT = TypeVar("StructuredResultT", bound=BaseModel)
STRUCTURED_OUTPUT_PROTOCOL_VERSION = "2026-08-20.v1"


def build_chat_model(
    model: str | None = None,
    *,
    temperature: float = 0.0,
    enable_thinking: bool | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    allow_env_fallback: bool = True,
) -> ChatOpenAI:
    """构建 ChatOpenAI。

    ``allow_env_fallback`` 默认开启以保持 CLI 和独立索引管线兼容。API Server
    应显式关闭它，确保模型管理目录是唯一配置来源。

    - 默认 ``temperature=0.0``、``enable_thinking=None``，不向供应商发送思考配置。
    - ``enable_thinking=True`` 同样沿用供应商默认行为，因为不同模型开启思考的参数
      并不统一。
    - 仅当 ``base_url`` 存在且 ``enable_thinking=False`` 时注入
      ``extra_body={"chat_template_kwargs": {"enable_thinking": False}}``：
      这是当前 OpenAI-compatible 后端使用的显式关闭方式。
    """
    if allow_env_fallback:
        api_key = api_key or get_openai_api_key()
    if not api_key:
        raise RuntimeError("未设置 OPENAI_API_KEY")
    if allow_env_fallback:
        base_url = base_url or get_openai_base_url()
        model = model or get_openai_model()
    if not model:
        raise RuntimeError("未配置模型名称")
    kwargs: dict = {
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
        "timeout": 300,
        # 让流式响应也回带 usage（OpenAI 的 stream_options.include_usage）。非流式
        # ``invoke`` 不受影响；流式路径据此读取 token 用量与缓存命中。
        "stream_usage": True,
    }
    if base_url:
        kwargs["base_url"] = base_url
        if enable_thinking is False:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    return ChatOpenAI(**kwargs)


def build_llm(
    model: str | None = None,
    *,
    temperature: float = 0.0,
    enable_thinking: bool | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    allow_env_fallback: bool = True,
    provider: str = "openai-compatible",
    max_schema_retries: int = 2,
    max_invoke_retries: int = 2,
    retry_base_delay_seconds: float = 0.25,
) -> LLMClient:
    """构建同时支持普通生成和结构化生成的统一 LLM 客户端。"""
    thinking_override = False if enable_thinking is False else None
    chat_model = build_chat_model(
        model,
        temperature=temperature,
        enable_thinking=thinking_override,
        api_key=api_key,
        base_url=base_url,
        allow_env_fallback=allow_env_fallback,
    )
    resolved_model = str(chat_model.model_name)
    return LLMClient(
        chat_model.ainvoke,
        metadata=LLMMetadata(
            provider=provider,
            model=resolved_model,
            temperature=temperature,
            enable_thinking=thinking_override,
            endpoint_identity=_endpoint_identity(chat_model.openai_api_base),
        ),
        max_schema_retries=max_schema_retries,
        max_invoke_retries=max_invoke_retries,
        retry_base_delay_seconds=retry_base_delay_seconds,
    )


def build_structured_chat_model(
    model: str | None = None,
    *,
    temperature: float = 0.0,
    enable_thinking: bool | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    allow_env_fallback: bool = True,
    provider: str = "openai-compatible",
    max_schema_retries: int = 2,
    max_invoke_retries: int = 2,
    retry_base_delay_seconds: float = 0.25,
) -> LLMClient:
    """Compatibility alias for callers that only need structured generation."""
    return build_llm(
        model,
        temperature=temperature,
        enable_thinking=enable_thinking,
        api_key=api_key,
        base_url=base_url,
        allow_env_fallback=allow_env_fallback,
        provider=provider,
        max_schema_retries=max_schema_retries,
        max_invoke_retries=max_invoke_retries,
        retry_base_delay_seconds=retry_base_delay_seconds,
    )


# ============ content 归一化 ============


def _join_blocks(blocks) -> str:
    """把 content blocks 列表拼成纯文本。

    兼容 str / dict / 对象元素；dict 先取 ``text`` 再取 ``content``（覆盖
    ``{"type": "text", "text": ...}`` 与 ``{"content": ...}`` 两种形态）；
    对象取 ``.text`` 属性。空串块跳过，块间用 ``\\n`` 连接（保留块边界）。
    """
    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            t = block.get("text")
            if not isinstance(t, str):
                t = block.get("content")
            if isinstance(t, str):
                parts.append(t)
        else:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(p for p in parts if p)


def content_to_text(resp) -> str:
    """从 LangChain 响应/消息/裸 content 提取文本。

    接受形态：
    - ``None`` / ``str``；
    - ``list``：content blocks 列表（每个 str/dict/对象），直接 ``_join_blocks``；
    - 对象 message：优先 ``.content_blocks``，再 ``.content``（str 或 list）。

    取不到文本时返回 ``""``（如思考模型 content 被吃空），**绝不返回对象
    repr 串**。
    """
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return _join_blocks(resp)
    blocks = getattr(resp, "content_blocks", None)
    if isinstance(blocks, list):
        t = _join_blocks(blocks)
        if t:
            return t
    content = getattr(resp, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        t = _join_blocks(content)
        if t:
            return t
    return ""


# ============ 结构化输出 ============


class StructuredLLM(Protocol):
    """Provider-neutral model boundary for validated Pydantic output."""

    async def generate_structured_output(
        self,
        *,
        prompt: str,
        schema: type[StructuredResultT],
    ) -> StructuredResultT: ...


class LLM(StructuredLLM, Protocol):
    """Unified model boundary for text and validated structured output."""

    async def generate(self, prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class LLMMetadata:
    provider: str
    model: str
    temperature: float
    enable_thinking: bool | None = None
    endpoint_identity: str = "custom"


@dataclass(frozen=True, slots=True)
class StructuredOutputTelemetry:
    strict_parse_failures: int = 0
    json_repair_attempt_count: int = 0
    json_repair_success_count: int = 0
    schema_retry_count: int = 0

    def merge(self, other: StructuredOutputTelemetry) -> StructuredOutputTelemetry:
        """Return the field-wise sum of two structured-output call records."""
        return StructuredOutputTelemetry(
            strict_parse_failures=(
                self.strict_parse_failures + other.strict_parse_failures
            ),
            json_repair_attempt_count=(
                self.json_repair_attempt_count + other.json_repair_attempt_count
            ),
            json_repair_success_count=(
                self.json_repair_success_count + other.json_repair_success_count
            ),
            schema_retry_count=self.schema_retry_count + other.schema_retry_count,
        )


@dataclass(frozen=True, slots=True)
class LLMCallTelemetry:
    """Statistics for a logical model call or an aggregate of such calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    model_attempts: int = 0
    invoke_retry_count: int = 0
    structured_output: StructuredOutputTelemetry = field(
        default_factory=StructuredOutputTelemetry
    )

    def merge(self, other: LLMCallTelemetry) -> LLMCallTelemetry:
        """Return aggregate telemetry while keeping field ownership in this layer."""
        return LLMCallTelemetry(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            model_attempts=self.model_attempts + other.model_attempts,
            invoke_retry_count=self.invoke_retry_count + other.invoke_retry_count,
            structured_output=self.structured_output.merge(other.structured_output),
        )


class StructuredOutputError(RuntimeError):
    """Raised when a response cannot be repaired into the requested schema."""

    code = "structured_output_invalid"
    public_message = "model output did not match the required schema"
    retryable = False


class ModelInvocationError(RuntimeError):
    """A provider failure represented without exposing provider exception text."""

    def __init__(self, *, code: str, public_message: str, retryable: bool) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.retryable = retryable


_EMPTY_CALL_TELEMETRY = LLMCallTelemetry()


@dataclass(slots=True)
class _InvocationStats:
    input_tokens: int = 0
    output_tokens: int = 0
    model_attempts: int = 0
    invoke_retry_count: int = 0


class LLMClient:
    """Unified text and structured-output client over an asynchronous model."""

    def __init__(
        self,
        invoke: Callable[[str], Awaitable[Any]],
        *,
        metadata: LLMMetadata,
        max_schema_retries: int = 2,
        max_invoke_retries: int = 2,
        retry_base_delay_seconds: float = 0.25,
    ) -> None:
        if max_schema_retries < 0 or max_invoke_retries < 0:
            raise ValueError("retry limits cannot be negative")
        if retry_base_delay_seconds < 0:
            raise ValueError("retry delay cannot be negative")
        self._invoke = invoke
        self.metadata = metadata
        self.max_schema_retries = max_schema_retries
        self.max_invoke_retries = max_invoke_retries
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.fingerprint_config = {
            "max_schema_retries": max_schema_retries,
            "max_invoke_retries": max_invoke_retries,
            "thinking_policy": (
                "disabled" if metadata.enable_thinking is False else "provider_default"
            ),
            "endpoint_identity": metadata.endpoint_identity,
            "structured_output_protocol_version": STRUCTURED_OUTPUT_PROTOCOL_VERSION,
        }
        self._last_call: ContextVar[LLMCallTelemetry] = ContextVar(
            f"llm_call_telemetry_{id(self)}", default=_EMPTY_CALL_TELEMETRY
        )

    def last_call_telemetry(self) -> LLMCallTelemetry:
        return self._last_call.get()

    async def generate(self, prompt: str) -> str:
        """Generate plain text without applying a structured-output contract."""
        invocation_stats = _InvocationStats()
        try:
            response = await self._invoke_with_retry(prompt, invocation_stats)
        except ModelInvocationError:
            self._set_telemetry(invocation_stats, StructuredOutputTelemetry())
            raise
        self._set_telemetry(invocation_stats, StructuredOutputTelemetry())
        return content_to_text(response)

    async def generate_structured_output(
        self,
        *,
        prompt: str,
        schema: type[StructuredResultT],
    ) -> StructuredResultT:
        structured_prompt = _structured_output_prompt(prompt, schema)
        current_prompt = structured_prompt
        structured_stats = StructuredOutputTelemetry()
        invocation_stats = _InvocationStats()
        for attempt in range(self.max_schema_retries + 1):
            try:
                response = await self._invoke_with_retry(
                    current_prompt, invocation_stats
                )
            except ModelInvocationError:
                self._set_telemetry(invocation_stats, structured_stats)
                raise
            raw = content_to_text(response)
            error: Exception | None = None
            parsed: object = None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                structured_stats = replace(
                    structured_stats,
                    strict_parse_failures=structured_stats.strict_parse_failures + 1,
                    json_repair_attempt_count=(
                        structured_stats.json_repair_attempt_count + 1
                    ),
                )
                try:
                    parsed = json.loads(repair_json(raw))
                    structured_stats = replace(
                        structured_stats,
                        json_repair_success_count=(
                            structured_stats.json_repair_success_count + 1
                        ),
                    )
                except json.JSONDecodeError as exc:
                    error = exc
            if error is None:
                try:
                    result = schema.model_validate(parsed)
                    self._set_telemetry(invocation_stats, structured_stats)
                    return result
                except ValidationError as exc:
                    error = exc
            if attempt == self.max_schema_retries:
                self._set_telemetry(invocation_stats, structured_stats)
                raise StructuredOutputError(
                    StructuredOutputError.public_message
                ) from error
            structured_stats = replace(
                structured_stats,
                schema_retry_count=structured_stats.schema_retry_count + 1,
            )
            current_prompt = _structured_output_correction_prompt(
                structured_prompt, schema, cast(Exception, error)
            )
        raise AssertionError("unreachable")

    async def _invoke_with_retry(
        self,
        prompt: str,
        stats: _InvocationStats,
    ) -> Any:
        for attempt in range(self.max_invoke_retries + 1):
            stats.model_attempts += 1
            if attempt:
                stats.invoke_retry_count += 1
            try:
                response = await self._invoke(prompt)
            except Exception as exc:
                code, public_message, retryable = classify_invoke_error(exc)
                if not retryable or attempt == self.max_invoke_retries:
                    raise ModelInvocationError(
                        code=code,
                        public_message=public_message,
                        retryable=retryable,
                    ) from exc
                await asyncio.sleep(self.retry_base_delay_seconds * (2**attempt))
                continue
            usage = _response_usage(response)
            stats.input_tokens += usage[0]
            stats.output_tokens += usage[1]
            return response
        raise AssertionError("unreachable")

    def _set_telemetry(
        self,
        invocation: _InvocationStats,
        structured_output: StructuredOutputTelemetry,
    ) -> None:
        self._last_call.set(
            LLMCallTelemetry(
                input_tokens=invocation.input_tokens,
                output_tokens=invocation.output_tokens,
                model_attempts=invocation.model_attempts,
                invoke_retry_count=invocation.invoke_retry_count,
                structured_output=structured_output,
            )
        )


def classify_invoke_error(exc: Exception) -> tuple[str, str, bool]:
    """Classify provider failures without exposing sensitive exception text."""
    status_code = _status_code(exc)
    name = type(exc).__name__.lower()
    detail = str(exc).lower()[:500]
    if status_code == 429 or "ratelimit" in name or "rate limit" in detail:
        return "model_rate_limited", "model provider rate limit exceeded", True
    if status_code == 408 or "timeout" in name or "timed out" in detail:
        return "model_timeout", "model provider request timed out", True
    if status_code == 409 or (status_code is not None and status_code >= 500):
        return "model_unavailable", "model provider is temporarily unavailable", True
    if "connection" in name or "connect" in name or "connection" in detail:
        return "model_connection_error", "model provider connection failed", True
    return "model_invocation_failed", "model provider request failed", False


def _endpoint_identity(base_url: str | None) -> str:
    if not base_url:
        return "openai-default"
    normalized = base_url.rstrip("/")
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    return f"sha256:{digest}"


def _status_code(exc: Exception) -> int | None:
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _response_usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, Mapping) and isinstance(response, Mapping):
        usage = response.get("usage_metadata")
    if isinstance(usage, Mapping):
        return (
            _non_negative_int(usage.get("input_tokens")),
            _non_negative_int(usage.get("output_tokens")),
        )

    metadata = getattr(response, "response_metadata", None)
    if not isinstance(metadata, Mapping) and isinstance(response, Mapping):
        metadata = response.get("response_metadata")
    token_usage = metadata.get("token_usage") if isinstance(metadata, Mapping) else None
    if isinstance(token_usage, Mapping):
        return (
            _non_negative_int(
                token_usage.get("input_tokens") or token_usage.get("prompt_tokens")
            ),
            _non_negative_int(
                token_usage.get("output_tokens") or token_usage.get("completion_tokens")
            ),
        )
    return 0, 0


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def _structured_output_correction_prompt(
    prompt: str,
    schema: type[BaseModel],
    error: Exception,
) -> str:
    details = str(error)[:1_500]
    return (
        f"{prompt}\n\nYour previous response was not valid for "
        f"{schema.__name__}: {details}\n"
        "Return only a corrected JSON object that follows the requested schema."
    )


def _structured_output_prompt(prompt: str, schema: type[BaseModel]) -> str:
    json_schema = json.dumps(
        schema.model_json_schema(), ensure_ascii=False, indent=2, default=str
    )
    return (
        f"{prompt}\n\nReturn only one JSON object. Do not use Markdown fences. "
        "It must validate against this JSON Schema:\n"
        f"{json_schema}"
    )
