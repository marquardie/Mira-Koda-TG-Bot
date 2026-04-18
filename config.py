"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file if present
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
MEETING_LINK: str = os.getenv("MEETING_LINK", "https://meet.google.com/your-room")

# Payment options shown to the client during the payment-method step.
PAYPAL_LINK: str = os.getenv("PAYPAL_LINK", "https://paypal.me/MiraKoda")
MONOBANK_CARD: str = os.getenv("MONOBANK_CARD", "0000 0000 0000 0000")
MONOBANK_NAME: str = os.getenv("MONOBANK_NAME", "Mira Koda")

# Slot-hold window: if the user picks a slot but never completes payment,
# the slot is auto-released after this many minutes.
SLOT_HOLD_MINUTES: int = 15

# Storage paths
STORAGE_DIR = BASE_DIR / "storage"
USERS_FILE = STORAGE_DIR / "users.json"
BOOKINGS_FILE = STORAGE_DIR / "bookings.json"
SLOTS_FILE = STORAGE_DIR / "slots.json"
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