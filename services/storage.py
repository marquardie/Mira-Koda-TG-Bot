"""Thin JSON-file storage for users, bookings, slots.

Kept intentionally simple: one JSON file per entity, loaded/saved on every
mutation. For an MVP with a handful of clients this is fine; swap for SQLite
later without changing handler signatures.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import BOOKINGS_FILE, SLOTS_FILE, STORAGE_DIR, USERS_FILE

# Single lock is enough — JSON mutations are tiny and infrequent.
_lock = threading.Lock()

# Canonical storage format for slot datetimes: ISO-like "YYYY-MM-DD HH:MM".
# Admin input uses a different human format — see ``parse_admin_slot_input``.
SLOT_FMT = "%Y-%m-%d %H:%M"

# Human-friendly format that the admin types into /slots_add.
# Example: "17.04.2026 - 14:00".
ADMIN_SLOT_INPUT_FMT = "%d.%m.%Y - %H:%M"

# Window for the 2 free late cancellations, measured from last_payment_date.
FREE_CANCEL_WINDOW_DAYS = 180
FREE_CANCELLATIONS_MAX = 2


def parse_slot(slot_str: str) -> datetime | None:
    """Parse a *stored* slot string (ISO ``YYYY-MM-DD HH:MM``).

    Returns ``None`` on bad input (no exception). Whitespace-tolerant.
    """
    if not slot_str:
        return None
    try:
        return datetime.strptime(slot_str.strip(), SLOT_FMT)
    except (ValueError, AttributeError):
        return None


def parse_admin_slot_input(raw: str) -> datetime | None:
    """Parse the admin's ``DD.MM.YYYY - HH:MM`` input.

    Tolerates extra internal whitespace (``"17.04.2026  -  14:00"``).
    Returns a naive datetime, or ``None`` if the input doesn't match or the
    calendar date / time is invalid.
    """
    if not raw:
        return None
    try:
        normalized = " ".join(raw.strip().split())
        return datetime.strptime(normalized, ADMIN_SLOT_INPUT_FMT)
    except (ValueError, AttributeError):
        return None


def to_storage_format(dt: datetime) -> str:
    """Render a datetime in the canonical storage format."""
    return dt.strftime(SLOT_FMT)


def _ensure_storage() -> None:
    for path, default in (
        (USERS_FILE, {}),
        (BOOKINGS_FILE, {"_seq": 0, "items": {}}),
        (SLOTS_FILE, {"_seq": 0, "items": {}}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            _write_json(path, default)


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user(user_id: int) -> dict[str, Any] | None:
    with _lock:
        users = _read_json(USERS_FILE)
    return users.get(str(user_id))


def save_user(user_id: int, data: dict[str, Any]) -> None:
    with _lock:
        users = _read_json(USERS_FILE)
        existing = users.get(str(user_id), {})
        existing.update(data)
        users[str(user_id)] = existing
        _write_json(USERS_FILE, users)


def user_exists(user_id: int) -> bool:
    """User is considered registered only after finishing the questionnaire."""
    u = get_user(user_id)
    return bool(u and u.get("questionnaire_done"))


def _user_defaults() -> dict[str, Any]:
    """Default profile fields. Applied lazily the first time we touch a user."""
    return {
        "sessions_completed": 0,
        "sessions_cancelled": 0,
        "free_cancellations_left": FREE_CANCELLATIONS_MAX,
        "available_sessions": 0,
        "last_payment_date": None,
        "package_offered": False,
    }


def _with_defaults(user: dict[str, Any]) -> dict[str, Any]:
    """Return ``user`` with any missing default fields filled in (non-mutating)."""
    merged = _user_defaults()
    merged.update(user)
    # Legacy: old installs stored session count under sessions_count. If the
    # new field is absent but the old one exists, migrate it silently.
    if "sessions_completed" not in user and "sessions_count" in user:
        merged["sessions_completed"] = user["sessions_count"]
    return merged


def increment_sessions_completed(user_id: int) -> int:
    """Increment sessions_completed and return the new value."""
    with _lock:
        users = _read_json(USERS_FILE)
        u = _with_defaults(users.get(str(user_id), {}))
        u["sessions_completed"] = u.get("sessions_completed", 0) + 1
        users[str(user_id)] = u
        _write_json(USERS_FILE, users)
        return u["sessions_completed"]


def increment_sessions_cancelled(user_id: int) -> int:
    with _lock:
        users = _read_json(USERS_FILE)
        u = _with_defaults(users.get(str(user_id), {}))
        u["sessions_cancelled"] = u.get("sessions_cancelled", 0) + 1
        users[str(user_id)] = u
        _write_json(USERS_FILE, users)
        return u["sessions_cancelled"]


def add_available_session(user_id: int, delta: int = 1) -> int:
    with _lock:
        users = _read_json(USERS_FILE)
        u = _with_defaults(users.get(str(user_id), {}))
        u["available_sessions"] = max(0, u.get("available_sessions", 0) + delta)
        users[str(user_id)] = u
        _write_json(USERS_FILE, users)
        return u["available_sessions"]


def consume_available_session(user_id: int) -> bool:
    """Atomically spend one credited session. Returns ``True`` on success."""
    with _lock:
        users = _read_json(USERS_FILE)
        u = _with_defaults(users.get(str(user_id), {}))
        if u.get("available_sessions", 0) <= 0:
            return False
        u["available_sessions"] -= 1
        users[str(user_id)] = u
        _write_json(USERS_FILE, users)
        return True


def refresh_free_cancellations(user_id: int) -> int:
    """Reset the late-cancel quota if the 6-month window has elapsed.

    Returns the current ``free_cancellations_left`` value after refresh.
    The window starts on ``last_payment_date``; if there's no payment yet,
    we simply return whatever is stored (default 2).
    """
    with _lock:
        users = _read_json(USERS_FILE)
        u = _with_defaults(users.get(str(user_id), {}))
        last_pay = u.get("last_payment_date")
        if last_pay:
            try:
                last_dt = datetime.fromisoformat(last_pay)
                if datetime.utcnow() - last_dt >= timedelta(days=FREE_CANCEL_WINDOW_DAYS):
                    u["free_cancellations_left"] = FREE_CANCELLATIONS_MAX
                    u["last_payment_date"] = datetime.utcnow().isoformat()
            except ValueError:
                pass
        users[str(user_id)] = u
        _write_json(USERS_FILE, users)
        return u.get("free_cancellations_left", FREE_CANCELLATIONS_MAX)


def consume_free_cancellation(user_id: int) -> int:
    """Decrement the late-cancel quota (never below 0). Returns new value."""
    with _lock:
        users = _read_json(USERS_FILE)
        u = _with_defaults(users.get(str(user_id), {}))
        u["free_cancellations_left"] = max(0, u.get("free_cancellations_left", FREE_CANCELLATIONS_MAX) - 1)
        users[str(user_id)] = u
        _write_json(USERS_FILE, users)
        return u["free_cancellations_left"]


def set_last_payment_date(user_id: int) -> None:
    """Stamp the user's last payment as 'now' (UTC ISO)."""
    with _lock:
        users = _read_json(USERS_FILE)
        u = _with_defaults(users.get(str(user_id), {}))
        u["last_payment_date"] = datetime.utcnow().isoformat()
        users[str(user_id)] = u
        _write_json(USERS_FILE, users)


def mark_package_offered(user_id: int) -> None:
    with _lock:
        users = _read_json(USERS_FILE)
        u = _with_defaults(users.get(str(user_id), {}))
        u["package_offered"] = True
        users[str(user_id)] = u
        _write_json(USERS_FILE, users)


def activate_package(user_id: int, sessions: int = 4) -> None:
    """Mark the user's package as active and credit them with `sessions`."""
    with _lock:
        users = _read_json(USERS_FILE)
        u = _with_defaults(users.get(str(user_id), {}))
        u["package_active"] = True
        u["available_sessions"] = u.get("available_sessions", 0) + sessions
        u["last_payment_date"] = datetime.utcnow().isoformat()
        users[str(user_id)] = u
        _write_json(USERS_FILE, users)


def is_package_active(user_id: int) -> bool:
    u = get_user_with_defaults(user_id)
    return bool(u and u.get("package_active"))


def get_user_with_defaults(user_id: int) -> dict[str, Any] | None:
    """Like ``get_user`` but guarantees all expected fields are present."""
    u = get_user(user_id)
    return _with_defaults(u) if u else None


def delete_user(user_id: int) -> bool:
    """Remove a user from storage and cancel their active bookings.

    Freed slots become available for rebooking.  Returns ``False`` if the
    user didn't exist.
    """
    with _lock:
        users = _read_json(USERS_FILE)
        if str(user_id) not in users:
            return False
        del users[str(user_id)]
        _write_json(USERS_FILE, users)
    for b in list_user_bookings(user_id):
        if b.get("status") in ("pending_payment", "waiting_confirm", "confirmed"):
            update_booking(b["id"], status="cancelled")
            mark_slot_booked(b["slot_id"], False)
    return True


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

def list_slots() -> list[dict[str, Any]]:
    with _lock:
        data = _read_json(SLOTS_FILE)
    items = [{"id": int(sid), **s} for sid, s in data["items"].items()]
    items.sort(key=lambda s: s["datetime"])
    return items


def list_free_slots() -> list[dict[str, Any]]:
    return [s for s in list_slots() if not s.get("booked")]


def add_slot(datetime_str: str) -> dict[str, Any]:
    """Add a slot. ``datetime_str`` must be ISO format: YYYY-MM-DD HH:MM."""
    with _lock:
        data = _read_json(SLOTS_FILE)
        data["_seq"] += 1
        slot_id = data["_seq"]
        data["items"][str(slot_id)] = {"datetime": datetime_str, "booked": False}
        _write_json(SLOTS_FILE, data)
        return {"id": slot_id, "datetime": datetime_str, "booked": False}


def delete_slot(slot_id: int) -> bool:
    with _lock:
        data = _read_json(SLOTS_FILE)
        if str(slot_id) not in data["items"]:
            return False
        del data["items"][str(slot_id)]
        _write_json(SLOTS_FILE, data)
        return True


def mark_slot_booked(slot_id: int, booked: bool = True) -> bool:
    with _lock:
        data = _read_json(SLOTS_FILE)
        s = data["items"].get(str(slot_id))
        if not s:
            return False
        s["booked"] = booked
        _write_json(SLOTS_FILE, data)
        return True


def try_book_slot(slot_id: int) -> bool:
    """Atomically reserve a slot.

    Returns ``True`` if the caller successfully claimed the slot,
    ``False`` if the slot is missing or was already booked by someone else.
    This closes the TOCTOU race between ``get_slot`` and ``mark_slot_booked``
    when two users confirm at the same time.
    """
    with _lock:
        data = _read_json(SLOTS_FILE)
        s = data["items"].get(str(slot_id))
        if not s or s.get("booked"):
            return False
        s["booked"] = True
        _write_json(SLOTS_FILE, data)
        return True


def get_slot(slot_id: int) -> dict[str, Any] | None:
    with _lock:
        data = _read_json(SLOTS_FILE)
    s = data["items"].get(str(slot_id))
    if s is None:
        return None
    return {"id": slot_id, **s}


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------
# Booking statuses: pending_payment → waiting_confirm → confirmed → done
#                   (any can transition to "cancelled")

def create_booking(user_id: int, slot_id: int) -> dict[str, Any]:
    with _lock:
        data = _read_json(BOOKINGS_FILE)
        data["_seq"] += 1
        booking_id = data["_seq"]
        booking = {
            "id": booking_id,
            "user_id": user_id,
            "slot_id": slot_id,
            "status": "pending_payment",
        }
        data["items"][str(booking_id)] = booking
        _write_json(BOOKINGS_FILE, data)
        return booking


def update_booking(booking_id: int, **fields: Any) -> dict[str, Any] | None:
    with _lock:
        data = _read_json(BOOKINGS_FILE)
        b = data["items"].get(str(booking_id))
        if not b:
            return None
        b.update(fields)
        _write_json(BOOKINGS_FILE, data)
        return b


def get_booking(booking_id: int) -> dict[str, Any] | None:
    with _lock:
        data = _read_json(BOOKINGS_FILE)
    return data["items"].get(str(booking_id))


def list_user_bookings(user_id: int) -> list[dict[str, Any]]:
    with _lock:
        data = _read_json(BOOKINGS_FILE)
    return [b for b in data["items"].values() if b["user_id"] == user_id]


def list_all_bookings() -> list[dict[str, Any]]:
    with _lock:
        data = _read_json(BOOKINGS_FILE)
    items = list(data["items"].values())
    items.sort(key=lambda b: b["id"])
    return items


# Initialize storage on import
_ensure_storage()
