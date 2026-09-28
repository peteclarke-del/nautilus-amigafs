"""Bound and redact text crossing desktop or diagnostic privacy boundaries."""

from __future__ import annotations

import re

MAX_USER_MESSAGE_CHARS = 1024
_ABSOLUTE_PATH = re.compile(r"(?<![\w:])/(?:[^:;,\]\[(){}\n])+")


def safe_user_message(value: object) -> str:
    """Remove absolute paths, controls and excessive text from untrusted detail."""

    text = "".join(
        character if character in "\n\t" or ord(character) >= 32 else "�"
        for character in str(value)
    )
    text = _ABSOLUTE_PATH.sub("<path>", text)
    if len(text) > MAX_USER_MESSAGE_CHARS:
        text = text[: MAX_USER_MESSAGE_CHARS - 1] + "…"
    return text


def safe_name(value: str, *, limit: int = 255) -> str:
    """Return a bounded basename suitable for an exported diagnostic report."""

    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character if ord(character) >= 32 else "�" for character in name)
    return cleaned[:limit]


__all__ = ["MAX_USER_MESSAGE_CHARS", "safe_name", "safe_user_message"]
