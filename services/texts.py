"""Loads all user-facing texts from local JSON and optional Google Sheets cache."""
from __future__ import annotations

import json
import logging
from typing import Any

from config import TEXTS_FILE

logger = logging.getLogger(__name__)

_local_texts: dict[str, str] = {}
_google_texts: dict[str, str] = {}


def _reload_local_texts() -> None:
    global _local_texts
    with open(TEXTS_FILE, "r", encoding="utf-8") as f:
        _local_texts = json.load(f)


def reload_texts() -> dict[str, int]:
    """Reload local texts and refresh Google Sheets cache if available."""
    global _google_texts

    _reload_local_texts()
    _google_texts = {}

    try:
        from services.google_texts import fetch_google_texts

        google_texts = fetch_google_texts()
        if google_texts:
            _google_texts = google_texts
            logger.info("loaded %s texts from Google Sheets", len(_google_texts))
        else:
            logger.info("Google Sheets texts are not configured or returned no rows; using local fallback")
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to load texts from Google Sheets; using local fallback: %s", exc)

    return {"local": len(_local_texts), "google": len(_google_texts)}


def get_text(key: str, **kwargs: Any) -> str:
    """Return a localized text by key with optional ``{placeholder}`` formatting.

    Falls back to the key itself if missing — makes typos visible during dev.
    """
    if not _local_texts:
        _reload_local_texts()

    template = _google_texts.get(key) or _local_texts.get(key) or f"[missing:{key}]"
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template


# Load local fallback on import
_reload_local_texts()
