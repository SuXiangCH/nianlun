"""Conservative token estimates for provider-agnostic agent context budgets."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from langchain_core.messages.utils import count_tokens_approximately

_CJK_CHARACTER_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def estimate_tokens(messages: Iterable[Any]) -> int:
    """Estimate tokens without undercounting Chinese text.

    LangChain's generic estimator assumes roughly four characters per token.
    That is too low for Chinese across common OpenAI-compatible tokenizers, so
    each CJK character contributes a conservative 1.5-token lower bound.
    """
    items = list(messages)
    approximate = count_tokens_approximately(items)
    text = "\n".join(str(getattr(item, "content", item)) for item in items)
    cjk_floor = (3 * len(_CJK_CHARACTER_RE.findall(text)) + 1) // 2
    return max(approximate, cjk_floor)


__all__ = ["estimate_tokens"]
