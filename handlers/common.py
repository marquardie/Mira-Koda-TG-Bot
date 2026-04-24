"""Shared keyboard builders and small formatting helpers.

Menu model:
* A **persistent ReplyKeyboard** at the bottom of the chat carries the main
  navigation (book / my bookings / profile / rules). It is always visible, so
  users never have to scroll to find buttons.
* **InlineKeyboard** buttons are used only for *actions* inside specific
  flows: picking a slot, confirming a cancel, choosing "back", etc.
"""
from __future__ import annotations

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from services import storage
from services.texts import get_text

# Callback-data for the inline "back to menu" button used across flows.
CB_BACK = "back"


def main_menu_keyboard(show_package: bool = False) -> ReplyKeyboardMarkup:
    """Persistent main-menu keyboard shown under every bot message.

    ``show_package=True`` adds the "💼 Оформити пакет" button (for returning
    clients or those with 3+ completed sessions).
    """
    rows = [
        [KeyboardButton(get_text("menu_book"))],
        [KeyboardButton(get_text("menu_my_bookings"))],
        [KeyboardButton(get_text("menu_profile"))],
        [KeyboardButton(get_text("menu_rules"))],
    ]
    if show_package:
        rows.insert(1, [KeyboardButton(get_text("menu_package"))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def menu_labels() -> set[str]:
    """Set of all main-menu button labels — used by the text router."""
    return {
        get_text("menu_book"),
        get_text("menu_my_bookings"),
        get_text("menu_profile"),
        get_text("menu_rules"),
        get_text("menu_package"),
    }


def user_can_see_package(user_id: int) -> bool:
    """True if user should see the package button (returning OR 3+ sessions)."""
    u = storage.get_user_with_defaults(user_id)
    if not u or u.get("package_active"):
        return False
    return u.get("client_type") == "returning" or u.get("sessions_completed", 0) >= 3


def main_menu_for(user_id: int) -> ReplyKeyboardMarkup:
    """Build the right menu for a specific user (with or without package btn)."""
    return main_menu_keyboard(show_package=user_can_see_package(user_id))


def back_button_row() -> list[InlineKeyboardButton]:
    """Reusable single-button row for inline flows."""
    return [InlineKeyboardButton(get_text("btn_back"), callback_data=CB_BACK)]


def confirm_cancel_keyboard(confirm_cb: str, cancel_cb: str) -> InlineKeyboardMarkup:
    """Two-button dialog (Confirm / Cancel) used by the slot-confirm step."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(get_text("btn_confirm"), callback_data=confirm_cb),
                InlineKeyboardButton(get_text("btn_cancel"), callback_data=cancel_cb),
            ],
            back_button_row(),
        ]
    )


STATUS_TEXT_KEYS = {
    "pending_payment": "status_pending",
    "waiting_confirm": "status_waiting_confirm",
    "confirmed": "status_confirmed",
    "done": "status_done",
    "cancelled": "status_cancelled",
}


def status_text(status: str) -> str:
    return get_text(STATUS_TEXT_KEYS.get(status, "status_pending"))


# ---------------------------------------------------------------------------
# Ukrainian-friendly date formatting
# ---------------------------------------------------------------------------

UA_WEEKDAYS = {
    0: "Понеділок",
    1: "Вівторок",
    2: "Середа",
    3: "Четвер",
    4: "Пʼятниця",
    5: "Субота",
    6: "Неділя",
}


def format_slot_human(slot_str: str) -> str:
    """Convert a raw slot string to "Понеділок 20.05 о 17:00"."""
    dt = storage.parse_slot(slot_str)
    if dt is None:
        return slot_str
    return f"{UA_WEEKDAYS[dt.weekday()]} {dt.strftime('%d.%m')} о {dt.strftime('%H:%M')}"
