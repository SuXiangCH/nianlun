"""Small request-language helpers for user-visible agent responses."""

from __future__ import annotations


def prefers_english(text: str) -> bool:
    """Return whether an English response is the best default for ``text``.

    English questions can contain Chinese names or product terms, so a single
    CJK character must not force Chinese output.  For non-empty text without
    CJK characters, retain the existing English-default behavior.
    """
    latin_letters = sum(char.isascii() and char.isalpha() for char in text)
    cjk_characters = sum("\u4e00" <= char <= "\u9fff" for char in text)
    return bool(text.strip()) and latin_letters >= cjk_characters


__all__ = ["prefers_english"]
