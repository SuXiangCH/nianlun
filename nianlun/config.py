"""Nianlun 的公共项目、环境和模型配置。"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - 依赖已声明，保留最小启动兼容

    def load_dotenv(*_args, **_kwargs):
        return False


load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "evals" / "results"

DEFAULT_MODEL = "deepseek-v4-flash-0731"


def get_openai_api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY")


def get_openai_base_url() -> str | None:
    """优先 OPENAI_BASE_URL，回退到常见的 OPENAI_API_BASE。"""
    return os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")


def get_openai_model() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)


def get_enable_thinking() -> bool:
    """读取 OpenAI-compatible 后端的思考模式开关。"""
    return os.environ.get("OPENAI_ENABLE_THINKING", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def get_openai_temperature() -> float:
    """读取采样温度，配置非法时回退到 0.8。"""
    raw = os.environ.get("OPENAI_TEMPERATURE", "0.8")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.8


def get_embedding_model() -> str:
    """读取语义检索兜底用的 embedding 模型。"""
    return os.environ.get("EMBEDDING_MODEL", "Qwen3-Embedding-0.6B")


__all__ = [
    "DEFAULT_MODEL",
    "PROJECT_ROOT",
    "RESULTS_DIR",
    "get_embedding_model",
    "get_enable_thinking",
    "get_openai_api_key",
    "get_openai_base_url",
    "get_openai_model",
    "get_openai_temperature",
]
