"""Small Server-Sent Events encoder used by the streaming chat endpoint."""

from __future__ import annotations

import json
from typing import Any


def encode_sse(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


__all__ = ["encode_sse"]
