"""Payment flows for sessions AND packages.

Session payment sequence:
    user picks slot → on_confirm reserves the slot →
    start_payment_method_picker shows PayPal / Monobank / Back →
    method chosen → method-specific instructions + "Я оплатив" button →
    user clicks → ask for ПІБ →
    user sends ПІБ → admin notified with method+ПІБ →
    admin confirms → +1 available_sessions, schedule reminder/session_end.

Package payment sequence (offered after the 3rd session):
    "💼 Оформити пакет" button → method picker → реквізити →
    "Я оплатив пакет" → ПІБ → admin → on confirm: activate_package(+4 sessions).

Both flows share a single ``awaiting`` slot (``payment_comment``) — the
``payment_kind`` flag in user_data tells the comment-handler whether it's a
session (``session``) or package (``package``) payment.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import ADMIN_ID, MONOBANK_CARD, MONOBANK_NAME, PAYPAL_LINK
from services import storage
from services.texts import get_text

logger = logging.getLogger(__name__)

# ---- session payment callbacks --------------------------------------------
CB_METHOD = "pmt_method:"           # pmt_method:<paypal|mono>:<booking_id>
CB_PAID = "paid:"                    # paid:<booking_id>
CB_ADMIN_CONFIRM = "adm_ok:"         # adm_ok:<booking_id>
CB_ADMIN_REJECT = "adm_no:"          # adm_no:<booking_id>

# ---- package payment callbacks --------------------------------------------
CB_PKG_BUY = "pkg_buy"
CB_PKG_METHOD = "pkg_method:"        # pkg_method:<paypal|mono>
CB_PKG_PAID = "pkg_paid"
CB_PKG_ADMIN_OK = "pkg_adm_ok:"      # pkg_adm_ok:<user_id>
CB_PKG_ADMIN_NO = "pkg_adm_no:"      # pkg_adm_no:<user_id>

# user_data keys used by the shared text router (handlers.booking.on_menu_text).
AWAIT_KEY = "awaiting"
AWAIT_PAYMENT_COMMENT = "payment_comment"
PAYMENT_BK_KEY = "payment_bk_id"
PAYMENT_KIND_KEY = "payment_kind"     # "session" | "package"
PAYMENT_METHOD_KEY = "payment_method" # cached for admin notice


# ---------------------------------------------------------------------------
# Session payment — method picker entry
# ---------------------------------------------------------------------------

async def start_payment_method_picker(
    update: Update, context: ContextTypes.DEFAULT_TYPE, booking_id: int
) -> None:
    """Show PayPal / Monobank / Back keyboard for a freshly-created booking."""
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(get_text("btn_pay_paypal"),
                                  callback_data=f"{CB_METHOD}paypal:{booking_id}")],
            [InlineKeyboardButton(get_text("btn_pay_monobank"),
                                  callback_data=f"{CB_METHOD}mono:{booking_id}")],
            [InlineKeyboardButton(get_text("btn_back"), callback_data="back")],
        ]
    )
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text=get_text("payment_method_prompt"),
        reply_markup=keyboard,
    )


def _method_label(method_key: str) -> str:
    return get_text("method_paypal") if method_key == "paypal" else get_text("method_monobank")


def _method_instructions(method_key: str, package: bool) -> str:
    if method_key == "paypal":
        key = "package_paypal" if package else "payment_paypal"
        return get_text(key, link=PAYPAL_LINK)
    key = "package_monobank" if package else "payment_monobank"
    return get_text(key, card=MONOBANK_CARD, name=MONOBANK_NAME)


async def on_method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User picked a payment method → show реквізити + 'Я оплатив' button."""
    query = update.callback_query
    await query.answer()

    payload = query.data.removeprefix(CB_METHOD)
    try:
        method_key, bk_str = payload.split(":")
        booking_id = int(bk_str)
    except ValueError:
        await query.edit_message_text(get_text("error_generic"))
        return

    booking = storage.get_booking(booking_id)
    if not booking or booking["status"] != "pending_payment":
        await query.edit_message_text(get_text("error_generic"))
        return

    # Persist the method choice on the booking so it's part of the audit trail.
    storage.update_booking(booking_id, payment_method=_method_label(method_key))

    text = _method_instructions(method_key, package=False)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(get_text("paid_button"),
                               callback_data=f"{CB_PAID}{booking_id}")]]
    )
    await query.edit_message_text(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Session payment — "Я оплатив" → ask for ПІБ
# ---------------------------------------------------------------------------

async def on_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    booking_id = int(query.data.removeprefix(CB_PAID))
    booking = storage.get_booking(booking_id)
    if not booking:
        await query.edit_message_text(get_text("error_generic"))
        return
    if booking["status"] != "pending_payment":
        await query.edit_message_text(get_text("payment_waiting_admin"))
        return

    context.user_data[AWAIT_KEY] = AWAIT_PAYMENT_COMMENT
    context.user_data[PAYMENT_BK_KEY] = booking_id
    context.user_data[PAYMENT_KIND_KEY] = "session"
    await query.edit_message_text(get_text("payment_comment_prompt"))


async def finalize_payment_comment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, comment: str
) -> None:
    """Called by handlers.booking text router with the user's ПІБ line.

    Branches by ``payment_kind`` (session vs package).
    """
    kind = context.user_data.pop(PAYMENT_KIND_KEY, "session")
    context.user_data[AWAIT_KEY] = None

    if kind == "package":
        await _finalize_package_comment(update, context, comment)
        return
    await _finalize_session_comment(update, context, comment)


async def _finalize_session_comment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, comment: str
) -> None:
    booking_id = context.user_data.pop(PAYMENT_BK_KEY, None)
    if not booking_id:
        return
    booking = storage.get_booking(booking_id)
    if not booking or booking["status"] != "pending_payment":
        await update.message.reply_text(get_text("payment_waiting_admin"))
        return

    storage.update_booking(booking_id, payment_comment=comment, status="waiting_confirm")
    logger.info("payment claimed booking=%s user=%s", booking_id, booking["user_id"])
    await update.message.reply_text(get_text("payment_comment_saved"))

    user = storage.get_user(booking["user_id"]) or {}
    slot = storage.get_slot(booking["slot_id"]) or {}
    method = booking.get("payment_method", "—")
    admin_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(get_text("admin_btn_confirm"),
                                     callback_data=f"{CB_ADMIN_CONFIRM}{booking_id}"),
                InlineKeyboardButton(get_text("admin_btn_reject"),
                                     callback_data=f"{CB_ADMIN_REJECT}{booking_id}"),
            ]
        ]
    )
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=get_text(
            "admin_new_payment",
            name=user.get("name", "—"),
            username=user.get("tg_username") or "—",
            user_id=booking["user_id"],
            age=user.get("age", "—"),
            request=user.get("request", "—"),
            method=method,
            payment_comment=comment,
            slot=slot.get("datetime", "—"),
            sessions_count=user.get("sessions_completed", user.get("sessions_count", 0)),
        ),
        reply_markup=admin_keyboard,
    )


# ---------------------------------------------------------------------------
# Admin confirm / reject for session payment
# ---------------------------------------------------------------------------

async def on_admin_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.common import format_slot_human
    from services.reminder import (
        cancel_slot_release,
        schedule_reminder,
        schedule_session_end,
    )

    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer(get_text("admin_only"), show_alert=True)
        return

    booking_id = int(query.data.removeprefix(CB_ADMIN_CONFIRM))
    booking = storage.get_booking(booking_id)
    if not booking:
        await query.answer(get_text("error_generic"), show_alert=True)
        return
    if booking["status"] != "waiting_confirm":
        await query.answer(get_text("admin_already_processed"), show_alert=True)
        return

    booking = storage.update_booking(booking_id, status="confirmed")
    storage.set_last_payment_date(booking["user_id"])
    storage.add_available_session(booking["user_id"], 1)
    cancel_slot_release(context.application, booking_id)
    logger.info("payment confirmed booking=%s user=%s", booking_id, booking["user_id"])
    await query.answer(get_text("admin_confirmed_toast"))

    slot = storage.get_slot(booking["slot_id"]) or {}
    slot_str = slot.get("datetime", "—")

    await query.edit_message_text(query.message.text + "\n\n" + get_text("status_confirmed"))

    await context.bot.send_message(
        chat_id=booking["user_id"],
        text=get_text("payment_confirmed", slot=format_slot_human(slot_str)),
    )
    await context.bot.send_message(chat_id=booking["user_id"], text=get_text("payment_confirmed_short"))
    await context.bot.send_message(chat_id=booking["user_id"], text=get_text("rules_text"))

    schedule_reminder(context.application, booking["user_id"], booking_id, slot_str)
    schedule_session_end(context.application, booking["user_id"], booking_id, slot_str)


async def on_admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.reminder import cancel_slot_release

    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer(get_text("admin_only"), show_alert=True)
        return

    booking_id = int(query.data.removeprefix(CB_ADMIN_REJECT))
    booking = storage.get_booking(booking_id)
    if not booking:
        await query.answer(get_text("error_generic"), show_alert=True)
        return
    if booking["status"] != "waiting_confirm":
        await query.answer(get_text("admin_already_processed"), show_alert=True)
        return

    booking = storage.update_booking(booking_id, status="cancelled")
    storage.mark_slot_booked(booking["slot_id"], booked=False)
    cancel_slot_release(context.application, booking_id)
    logger.info("payment rejected booking=%s user=%s", booking_id, booking["user_id"])
    await query.answer(get_text("admin_rejected_toast"))

    await query.edit_message_text(query.message.text + "\n\n" + get_text("status_cancelled"))
    await context.bot.send_message(chat_id=booking["user_id"], text=get_text("admin_rejected_user"))


# ---------------------------------------------------------------------------
# Package flow
# ---------------------------------------------------------------------------

def package_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(get_text("btn_buy_package"), callback_data=CB_PKG_BUY)]]
    )


async def on_package_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User clicked '💼 Оформити пакет' — show method picker for the package."""
    query = update.callback_query
    await query.answer()

    if storage.is_package_active(update.effective_user.id):
        await query.edit_message_text(get_text("package_already_active"))
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(get_text("btn_pay_paypal"),
                                  callback_data=f"{CB_PKG_METHOD}paypal")],
            [InlineKeyboardButton(get_text("btn_pay_monobank"),
                                  callback_data=f"{CB_PKG_METHOD}mono")],
            [InlineKeyboardButton(get_text("btn_back"), callback_data="back")],
        ]
    )
    await query.edit_message_text(get_text("package_method_prompt"), reply_markup=keyboard)


async def on_package_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    method_key = query.data.removeprefix(CB_PKG_METHOD)

    context.user_data[PAYMENT_METHOD_KEY] = _method_label(method_key)
    text = _method_instructions(method_key, package=True)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(get_text("btn_paid_package"), callback_data=CB_PKG_PAID)]]
    )
    await query.edit_message_text(text, reply_markup=keyboard)


async def on_package_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if storage.is_package_active(update.effective_user.id):
        await query.edit_message_text(get_text("package_already_active"))
        return

    # Arm the text router for the ПІБ input — branch=package.
    context.user_data[AWAIT_KEY] = AWAIT_PAYMENT_COMMENT
    context.user_data[PAYMENT_KIND_KEY] = "package"
    await query.edit_message_text(get_text("package_comment_prompt"))


async def _finalize_package_comment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, comment: str
) -> None:
    user = storage.get_user(update.effective_user.id) or {}
    method = context.user_data.pop(PAYMENT_METHOD_KEY, "—")

    await update.message.reply_text(get_text("package_received_pending"))

    admin_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(get_text("admin_btn_confirm_package"),
                                     callback_data=f"{CB_PKG_ADMIN_OK}{update.effective_user.id}"),
                InlineKeyboardButton(get_text("admin_btn_reject_package"),
                                     callback_data=f"{CB_PKG_ADMIN_NO}{update.effective_user.id}"),
            ]
        ]
    )
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=get_text(
            "admin_package_new",
            username=user.get("tg_username") or "—",
            user_id=update.effective_user.id,
            name=user.get("name", "—"),
            method=method,
            comment=comment,
        ),
        reply_markup=admin_keyboard,
    )
    logger.info("package claimed user=%s method=%s", update.effective_user.id, method)


async def on_package_admin_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer(get_text("admin_only"), show_alert=True)
        return
    user_id = int(query.data.removeprefix(CB_PKG_ADMIN_OK))
    storage.activate_package(user_id, sessions=4)
    await query.answer(get_text("admin_package_confirmed_toast"))
    await query.edit_message_text(query.message.text + "\n\n✅")
    try:
        await context.bot.send_message(chat_id=user_id, text=get_text("package_activated"))
    except Exception:  # noqa: BLE001
        logger.exception("package activate notify failed user=%s", user_id)


async def on_package_admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer(get_text("admin_only"), show_alert=True)
        return
    user_id = int(query.data.removeprefix(CB_PKG_ADMIN_NO))
    await query.answer(get_text("admin_package_rejected_toast"))
    await query.edit_message_text(query.message.text + "\n\n❌")
    try:
        await context.bot.send_message(chat_id=user_id, text=get_text("package_rejected_user"))
    except Exception:  # noqa: BLE001
        logger.exception("package reject notify failed user=%s", user_id)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(app) -> None:
    app.add_handler(CallbackQueryHandler(on_method_chosen, pattern=f"^{CB_METHOD}"))
    app.add_handler(CallbackQueryHandler(on_paid, pattern=f"^{CB_PAID}"))
    app.add_handler(CallbackQueryHandler(on_admin_confirm, pattern=f"^{CB_ADMIN_CONFIRM}"))
    app.add_handler(CallbackQueryHandler(on_admin_reject, pattern=f"^{CB_ADMIN_REJECT}"))

    # Package flow
    app.add_handler(CallbackQueryHandler(on_package_admin_confirm, pattern=f"^{CB_PKG_ADMIN_OK}"))
    app.add_handler(CallbackQueryHandler(on_package_admin_reject, pattern=f"^{CB_PKG_ADMIN_NO}"))
    app.add_handler(CallbackQueryHandler(on_package_method, pattern=f"^{CB_PKG_METHOD}"))
    app.add_handler(CallbackQueryHandler(on_package_paid, pattern=f"^{CB_PKG_PAID}$"))
    app.add_handler(CallbackQueryHandler(on_package_buy, pattern=f"^{CB_PKG_BUY}$"))