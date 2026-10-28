"""Bounded provisional text and committed image-aware stream output."""

from __future__ import annotations

import re
from collections.abc import Iterator

from app.api_server.common.errors import ApiError


def utf8_chunks(text: str, maximum: int = 16 * 1024) -> Iterator[str]:
    """Split transport text on codepoint boundaries, including Base64 output."""
    if maximum < 4:
        raise ValueError("UTF-8 chunk limit must be at least four bytes")
    data = text.encode("utf-8")
    start = 0
    while start < len(data):
        end = min(start + maximum, len(data))
        while end < len(data) and data[end] & 0xC0 == 0x80:
            end -= 1
        yield data[start:end].decode("utf-8")
        start = end


class ImageStreamPrefix:
    """Emit only complete unambiguous lines, freezing at the first candidate.

    A conservative boundary avoids automatic citation images, future definitions,
    HTML/table normalization and code fences changing already-sent bytes. The
    worker remains consumed after freezing; only the final projection supplies
    the suffix.
    """

    def __init__(self, *, max_canonical_bytes: int = 1024 * 1024) -> None:
        self.maximum = max_canonical_bytes
        self._size = 0
        self._line = ""
        self._sent = ""
        self._frozen = False

    def feed(self, delta: str) -> str:
        self._size += len(delta.encode("utf-8"))
        if self._size > self.maximum:
            raise ApiError("图片回答正文超出大小限制", 413)
        if self._frozen:
            return ""
        self._line += delta
        output: list[str] = []
        while "\n" in self._line:
            line, self._line = self._line.split("\n", 1)
            # Labels can span lines, and list/quote prefixes can hide definitions.
            reference_candidate = re.match(
                r"^[ \t]*(?:>[ \t]*|[-+*][ \t]+|[0-9]{1,9}[.)][ \t]+)*\[",
                line,
            )
            if (
                any(char in line for char in "!<`~|\r&\\")
                or re.search(r"\[\d+\]", line)
                or reference_candidate
            ):
                self._frozen = True
                self._line = ""
                break
            output.append(line + "\n")
        text = "".join(output)
        self._sent += text
        return text

    def remainder(self, committed: str) -> str:
        if not committed.startswith(self._sent):
            raise ApiError("流式答案与已提交结果不一致", 502)
        return committed[len(self._sent) :]
