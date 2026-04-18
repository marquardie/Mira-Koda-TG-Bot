"""Loads all user-facing texts from texts.json.

Texts are read once on import and cached. In dev you can call ``reload_texts()``
to pick up edits without restarting the bot.
"""
from __future__ import annotations

import json
from typing import Any

from config import TEXTS_FILE

_texts: dict[str, str] = {}


def reload_texts() -> None:
    """Reload texts from disk."""
    global _texts
    with open(TEXTS_FILE, "r", encoding="utf-8") as f:
        _texts = json.load(f)


def get_text(key: str, **kwargs: Any) -> str:
    """Return a localized text by key with optional ``{placeholder}`` formatting.

    Falls back to the key itself if missing — makes typos visible during dev.
    """
    if not _texts:
        reload_texts()
    template = _texts.get(key, f"[missing:{key}]")
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template


# Load on import
reload_texts()