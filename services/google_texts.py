"""Google Sheets loader for bot texts."""
from __future__ import annotations

import json
from urllib.parse import quote

from config import GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEETS_ID, GOOGLE_TEXTS_RANGE

GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def fetch_google_texts() -> dict[str, str]:
    """Load texts from Google Sheets.

    Supports both sheet layouts:
    * key | text
    * description | key | text

    Returns an empty dict if Google Sheets is not configured.
    Raises on transport/auth/API errors so the caller can log a warning and
    keep using local fallback texts.
    """
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return {}

    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=[GOOGLE_SHEETS_SCOPE])
    session = AuthorizedSession(creds)

    range_ref = quote(GOOGLE_TEXTS_RANGE, safe="!:'")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEETS_ID}/values/{range_ref}"
    response = session.get(url, timeout=20)
    response.raise_for_status()

    rows = response.json().get("values", [])
    key_idx = 0
    text_idx = 1

    if rows:
        header = [str(cell).strip().lower() for cell in rows[0]]
        if len(header) >= 3 and header[:3] == ["description", "key", "text"]:
            key_idx = 1
            text_idx = 2
            rows = rows[1:]
        elif len(header) >= 2 and header[:2] == ["key", "text"]:
            key_idx = 0
            text_idx = 1
            rows = rows[1:]

    texts: dict[str, str] = {}
    for row in rows:
        if not row:
            continue
        key = str(row[key_idx]).strip() if len(row) > key_idx else ""
        text = str(row[text_idx]) if len(row) > text_idx else ""
        if key:
            texts[key] = text
    return texts
