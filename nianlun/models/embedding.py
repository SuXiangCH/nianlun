"""Embedding 工厂与响应归一化的统一入口，检索侧与索引侧共用。"""

from __future__ import annotations

import base64
import json
import re
import struct
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import httpx

from nianlun.config import (
    get_embedding_model,
    get_openai_api_key,
    get_openai_base_url,
)

from langchain_openai import OpenAIEmbeddings

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _base64_vector(value: str) -> list[float] | None:
    """Decode a base64 float32 vector string.

    The OpenAI SDK requests ``encoding_format=base64`` by default, so some
    OpenAI-compatible gateways return the whole vector as a bare base64 string
    body (or as a string-valued field) instead of the standard JSON envelope.
    Only attempt decoding for strings that look like base64 to avoid mangling
    JSON bodies.
    """
    text = value.strip()
    if not text or _BASE64_RE.fullmatch(text) is None:
        return None
    try:
        encoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
    except Exception:
        return None
    if not encoded or len(encoded) % 4:
        return None
    try:
        return list(struct.unpack("<%df" % (len(encoded) // 4), encoded))
    except Exception:
        return None


def _vector_list(value: object) -> list[float] | None:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            return _vector_list(parsed)
        if _is_number(parsed):
            return None
        return _base64_vector(text)
    if (
        not isinstance(value, list)
        or not value
        or not all(_is_number(item) for item in value)
    ):
        return None
    return [float(item) for item in value]


def _vectors(value: object) -> list[list[float]] | None:
    vector = _vector_list(value)
    if vector is not None:
        return [vector]
    if not isinstance(value, list) or not value:
        return None
    result = [_vector_list(item) for item in value]
    if any(item is None for item in result):
        return None
    return [item for item in result if item is not None]


def _embedding_vectors(payload: object) -> list[list[float]] | None:
    """Extract vectors from common OpenAI-compatible response variants."""
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if parsed is None:
            return _vectors(payload)
        return _embedding_vectors(parsed)
    vectors = _vectors(payload)
    if vectors is not None:
        return vectors
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if isinstance(data, dict):
        nested = _embedding_vectors(data)
        if nested is not None:
            return nested
    if isinstance(data, list) and data:
        if all(isinstance(item, dict) for item in data):
            result = [_vector_list(item.get("embedding")) for item in data]
            if all(item is not None for item in result):
                return [item for item in result if item is not None]
        vectors = _vectors(data)
        if vectors is not None:
            return vectors
    for key in ("embedding", "vector", "data"):
        vector = _vector_list(payload.get(key))
        if vector is not None:
            return [vector]
    return None


def _standard_embedding_response(
    payload: object, model: str, *, original: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    vectors = _embedding_vectors(payload)
    if vectors is None:
        return None
    response = dict(original or {})
    response.update(
        {
            "object": "list",
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ],
            "model": str(response.get("model") or model),
            "usage": response.get("usage", {"prompt_tokens": 0, "total_tokens": 0}),
        }
    )
    return response


def embedding_response_hook(model: str) -> Callable[[httpx.Response], None]:
    """Normalize non-standard successful embedding response bodies.

    Some OpenAI-compatible gateways return a JSON vector as a JSON string, a
    bare base64 float32 string (the SDK requests ``encoding_format=base64`` by
    default), or omit the OpenAI response envelope. OpenAI SDK 2.x then passes
    the string to its embedding post-parser, which raises ``'str' object has no
    attribute 'data'``. Normalize those successful responses before the SDK
    parses them.
    """

    def normalize(response: httpx.Response) -> None:
        if response.status_code >= 400 or not response.request.url.path.endswith(
            "/embeddings"
        ):
            return
        try:
            response.read()
            text = response.content.decode("utf-8", errors="replace").strip()
        except (httpx.HTTPError, UnicodeDecodeError):
            return
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            payload = text
        original = payload if isinstance(payload, dict) else None
        normalized = _standard_embedding_response(payload, model, original=original)
        if normalized is None:
            return
        setattr(response, "_content", json.dumps(normalized).encode("utf-8"))
        response.headers["content-type"] = "application/json"

    return normalize


def _embedding_http_client(model: str) -> httpx.Client:
    return httpx.Client(
        timeout=300.0,
        event_hooks={"response": [embedding_response_hook(model)]},
    )


def build_embeddings_model(
    model: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    dimensions: int | None = None,
    check_ctx_length: bool = False,
    allow_env_fallback: bool = True,
    http_client: httpx.Client | None = None,
) -> OpenAIEmbeddings:
    """构建 ``OpenAIEmbeddings``。

    ``allow_env_fallback`` 默认开启以保持 CLI 和独立索引管线兼容。API Server
    应显式关闭它，确保模型管理目录是唯一配置来源。

    与 ``build_chat_model`` 对称。注意 ``OpenAIEmbeddings`` 字段名为
    ``openai_api_key`` / ``openai_api_base``（非 ``api_key`` / ``base_url``），
    故未直接复用 ``llm.py`` 的构造逻辑。

    - ``model`` 默认 ``get_embedding_model()``（``text-embedding-3-small``）。
    - ``base_url`` 存在时透传给 ``openai_api_base``（与 chat 侧共用同一中转站）。
    - ``dimensions`` 仅 ``text-embedding-3-*`` 系列生效，缩短向量省存储 / 加速检索；
      其它模型传该参数会被后端拒，调用方自行确保模型支持。
    - ``check_ctx_length`` 默认 ``False``：关掉 langchain 的 ctx 长度检查（其走 tiktoken
      分词，需联网下载词表且为 OpenAI 专属分词器，离线 / 非 OpenAI 场景会出错）。
      在线且确定用 OpenAI ``text-embedding-3-*`` 时传 ``True``：单条超
      ``embedding_ctx_length``（8191）时按段拆分嵌入再按 token 数加权平均成一条向量，
      避免被后端拒。
    """
    if allow_env_fallback:
        api_key = api_key or get_openai_api_key()
    if not api_key:
        raise RuntimeError("未设置 OPENAI_API_KEY")
    if allow_env_fallback:
        base_url = base_url or get_openai_base_url()
        model = model or get_embedding_model()
    if not model:
        raise RuntimeError("未配置 Embedding 模型名称")
    kwargs: dict = {
        "model": model,
        "openai_api_key": api_key,
        "check_embedding_ctx_length": check_ctx_length,
    }
    if base_url:
        kwargs["openai_api_base"] = base_url
    if dimensions is not None:
        kwargs["dimensions"] = dimensions
    kwargs["http_client"] = http_client or _embedding_http_client(model)
    return OpenAIEmbeddings(**kwargs)


class TextEmbedder(Protocol):
    """Minimum interface required by vector indexing and semantic retrieval."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def build_embedding_client(
    model: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    dimensions: int | None = None,
    allow_env_fallback: bool = True,
) -> TextEmbedder:
    """Build the OpenAI-compatible embedding client used by the application."""
    return build_embeddings_model(
        model=model,
        api_key=api_key,
        base_url=base_url,
        dimensions=dimensions,
        allow_env_fallback=allow_env_fallback,
    )


def embed_records(
    records: Sequence[dict],
    embedder: TextEmbedder,
) -> list[dict]:
    """Embed records while preserving metadata and removing input text."""
    if not records:
        return []
    texts = [str(record["embed_text"]) for record in records]
    vectors = embedder.embed_documents(texts)
    if len(vectors) != len(records):
        raise RuntimeError(
            f"embedding 返回数量不一致: records={len(records)}, vectors={len(vectors)}"
        )
    result: list[dict] = []
    for record, vector in zip(records, vectors, strict=True):
        item = {key: value for key, value in record.items() if key != "embed_text"}
        item["vector"] = vector
        result.append(item)
    return result


__all__ = [
    "TextEmbedder",
    "build_embedding_client",
    "build_embeddings_model",
    "embed_records",
    "embedding_response_hook",
]
