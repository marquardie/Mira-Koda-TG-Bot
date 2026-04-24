"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file if present
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_path(name: str, default: str) -> Path:
    raw = os.getenv(name, default)
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
MEETING_LINK: str = os.getenv("MEETING_LINK", "https://meet.google.com/your-room")
GOOGLE_SHEETS_ID: str = os.getenv("GOOGLE_SHEETS_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_TEXTS_RANGE: str = os.getenv("GOOGLE_TEXTS_RANGE", "texts!A:C")
GOOGLE_TEXTS_REFRESH_MINUTES: int = int(os.getenv("GOOGLE_TEXTS_REFRESH_MINUTES", "1"))

# Payment options shown to the client during the payment-method step.
PAYPAL_LINK: str = os.getenv("PAYPAL_LINK", "https://paypal.me/MiraKoda")
MONOBANK_CARD: str = os.getenv("MONOBANK_CARD", "0000 0000 0000 0000")
MONOBANK_NAME: str = os.getenv("MONOBANK_NAME", "Mira Koda")

# Slot-hold window: if the user picks a slot but never completes payment,
# the slot is auto-released after this many minutes.
SLOT_HOLD_MINUTES: int = 30

# Storage paths
USERS_FILE = _env_path("USERS_FILE_PATH", os.getenv("DATA_FILE_PATH", "data/users.json"))
BOOKINGS_FILE = _env_path("BOOKINGS_FILE_PATH", "data/bookings.json")
SLOTS_FILE = _env_path("SLOTS_FILE_PATH", "data/slots.json")
STORAGE_DIR = USERS_FILE.parent
TEXTS_FILE = BASE_DIR / "texts.json"

# Reminder offset in minutes before session
REMINDER_MINUTES_BEFORE = 30

# Working hours (Kyiv local time). Client messages outside this window get
# the "off hours" auto-reply and are NOT forwarded to the admin.
WORKING_HOURS_START = 9   # 09:00
WORKING_HOURS_END = 19    # 19:00


def ensure_config() -> None:
    """Fail fast if critical env vars are missing."""
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID is not set. Put your Telegram user id into .env.")
