"""Booking, cancel, reschedule and main-menu text routing.

Flow map
========
/book OR main-menu "📅 Записатися на сесію"  → show_slots
slot:<id>                                    → on_slot_chosen (confirm dialog)
bkconfirm:<id>                               → on_confirm
bkcancel                                     → on_slot_dialog_cancel
/my OR main-menu "📂 Мої записи"             → my_bookings (cards)
bk_cancel:<bid>                              → ask_cancel_reason (asks ПІБ-style reason)
bk_cancel_ok:<bid>                           → do_cancel (commit)
bk_keep:<bid>                                → keep_booking
bk_resch:<bid>                               → ask_reschedule (free slots or
                                               "not allowed" + fallback cancel btn)
bk_resch_pick:<bid>:<sid>                    → do_reschedule
back                                         → on_back (return to main menu)

The module also owns the **text router** (``on_menu_text``) — a single
MessageHandler that:
* matches the 4 main-menu button labels and dispatches them,
* intercepts awaited text input (cancel reason / payment ПІБ) via
  ``context.user_data``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ADMIN_ID
from handlers.common import (
    CB_BACK,
    back_button_row,
    confirm_cancel_keyboard,
    format_slot_human,
    main_menu_keyboard,
    menu_labels,
    status_text,
)
from services import storage
from services.texts import get_text

logger = logging.getLogger(__name__)

# Callback prefixes (short — Telegram callback_data is capped at 64 bytes).
CB_SLOT = "slot:"
CB_CONFIRM = "bkconfirm:"
CB_CANCEL_DIALOG = "bkcancel"
CB_CANCEL_BK = "bk_cancel:"
CB_CANCEL_OK = "bk_cancel_ok:"
CB_KEEP_BK = "bk_keep:"
CB_RESCH = "bk_resch:"
CB_RESCH_PICK = "bk_resch_pick:"

# user_data keys for the cancel-reason intercept.
AWAIT_KEY = "awaiting"
AWAIT_CANCEL_REASON = "cancel_reason"
CANCEL_BK_KEY = "cancel_bk_id"

# 12-hour threshold — inside this window free cancel costs a quota credit.
CANCEL_FREE_HOURS = 12

# Kyiv local time (matches services/reminder.py).
KYIV_OFFSET = timezone(timedelta(hours=3))

# Statuses we consider "actionable" (can be cancelled/rescheduled).
ACTIONABLE_STATUSES = {"pending_payment", "waiting_confirm", "confirmed"}


def _slot_datetime_kyiv(slot_str: str):
    naive = storage.parse_slot(slot_str)
    return naive.replace(tzinfo=KYIV_OFFSET) if naive else None


def _hours_until_slot(slot_str: str) -> float:
    dt = _slot_datetime_kyiv(slot_str)
    if dt is None:
        return 0.0
    return (dt - datetime.now(tz=KYIV_OFFSET)).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Booking — show slots → confirm → payment
# ---------------------------------------------------------------------------

async def show_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List free slots as inline buttons with a Back-row."""
    if not storage.user_exists(update.effective_user.id):
        await update.effective_chat.send_message(get_text("welcome_new"))
        return

    free = storage.list_free_slots()
    if not free:
        await update.effective_chat.send_message(get_text("no_slots"))
        return

    keyboard = [
        [InlineKeyboardButton(format_slot_human(s["datetime"]), callback_data=f"{CB_SLOT}{s['id']}")]
        for s in free
    ]
    keyboard.append(back_button_row())
    await update.effective_chat.send_message(
        get_text("choose_slot"),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_slot_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    slot_id = int(query.data.removeprefix(CB_SLOT))
    slot = storage.get_slot(slot_id)
    if not slot or slot["booked"]:
        await query.edit_message_text(get_text("slot_taken"))
        return

    await query.edit_message_text(
        get_text("confirm_slot", slot=format_slot_human(slot["datetime"])),
        reply_markup=confirm_cancel_keyboard(f"{CB_CONFIRM}{slot_id}", CB_CANCEL_DIALOG),
    )


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reserve the slot, start a 15-min hold, and ask for the payment method.

    If the user has a prepaid session on balance — skip payment entirely.
    """
    from config import SLOT_HOLD_MINUTES
    from handlers.payment import start_payment_method_picker
    from services.reminder import schedule_reminder, schedule_session_end, schedule_slot_release

    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    slot_id = int(query.data.removeprefix(CB_CONFIRM))
    if not storage.try_book_slot(slot_id):
        await query.edit_message_text(get_text("slot_taken"))
        return

    slot = storage.get_slot(slot_id)
    slot_human = format_slot_human(slot["datetime"])

    # Credit path — prepaid session from a previous cancellation OR package.
    if storage.consume_available_session(user_id):
        booking = storage.create_booking(user_id, slot_id)
        storage.update_booking(booking["id"], status="confirmed")
        logger.info("booking via credit id=%s user=%s slot=%s", booking["id"], user_id, slot["datetime"])
        await query.edit_message_text(get_text("booking_free_via_credit", slot=slot_human))
        schedule_reminder(context.application, user_id, booking["id"], slot["datetime"])
        schedule_session_end(context.application, user_id, booking["id"], slot["datetime"])
        return

    booking = storage.create_booking(user_id, slot_id)
    logger.info("booking created id=%s user=%s slot=%s", booking["id"], user_id, slot["datetime"])

    # Hold the slot — auto-release after SLOT_HOLD_MINUTES if user never pays.
    schedule_slot_release(context.application, booking["id"], SLOT_HOLD_MINUTES)

    await query.edit_message_text(
        get_text("booking_reserved", slot=slot_human, hold_min=SLOT_HOLD_MINUTES)
    )
    await start_payment_method_picker(update, context, booking_id=booking["id"])


async def on_slot_dialog_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(get_text("booking_cancelled"))


# ---------------------------------------------------------------------------
# "Мої записи" — one card per booking, ALWAYS shows cancel+reschedule
# ---------------------------------------------------------------------------

async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    bookings = storage.list_user_bookings(user_id)
    if not bookings:
        await update.effective_chat.send_message(get_text("my_bookings_empty"))
        return

    await update.effective_chat.send_message(get_text("my_bookings_header"))
    for b in sorted(bookings, key=lambda x: x["id"]):
        slot = storage.get_slot(b["slot_id"]) or {}
        slot_str = slot.get("datetime", "—")
        card = get_text("my_bookings_card", slot=format_slot_human(slot_str), status=status_text(b["status"]))

        markup = None
        if b["status"] in ACTIONABLE_STATUSES and slot_str != "—" and _hours_until_slot(slot_str) > 0:
            # Spec: always show BOTH buttons on future active bookings.
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            get_text("btn_reschedule_booking"), callback_data=f"{CB_RESCH}{b['id']}"
                        ),
                        InlineKeyboardButton(
                            get_text("btn_cancel_booking"), callback_data=f"{CB_CANCEL_BK}{b['id']}"
                        ),
                    ]
                ]
            )
        await update.effective_chat.send_message(card, reply_markup=markup)


# ---------------------------------------------------------------------------
# Cancel flow — step 1: ask for reason
# ---------------------------------------------------------------------------

async def ask_cancel_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry to cancel flow. Ask the user for a reason before confirming."""
    query = update.callback_query
    await query.answer()

    booking_id = int(query.data.removeprefix(CB_CANCEL_BK))
    booking = storage.get_booking(booking_id)
    if not booking or booking["user_id"] != update.effective_user.id:
        await query.edit_message_text(get_text("error_generic"))
        return
    if booking["status"] not in ACTIONABLE_STATUSES:
        await query.edit_message_text(get_text("cancel_kept"))
        return

    # Arm the text router to capture the next plain-text message as the reason.
    context.user_data[AWAIT_KEY] = AWAIT_CANCEL_REASON
    context.user_data[CANCEL_BK_KEY] = booking_id
    await query.edit_message_text(get_text("cancel_reason_prompt"))


async def _show_cancel_confirm(update: Update, booking_id: int) -> None:
    """Step 2: show the warning dialog with Confirm/Keep buttons."""
    booking = storage.get_booking(booking_id)
    if not booking:
        await update.effective_chat.send_message(get_text("error_generic"))
        return
    slot = storage.get_slot(booking["slot_id"]) or {}
    slot_str = slot.get("datetime", "")
    slot_human = format_slot_human(slot_str)
    hours_left = _hours_until_slot(slot_str)

    if hours_left >= CANCEL_FREE_HOURS:
        prompt = get_text("cancel_confirm_free", slot=slot_human)
    else:
        free_left = storage.refresh_free_cancellations(booking["user_id"])
        if free_left > 0:
            prompt = get_text("cancel_confirm_with_quota", slot=slot_human, left=free_left)
        else:
            prompt = get_text("cancel_confirm_warning")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    get_text("btn_cancel_confirm"), callback_data=f"{CB_CANCEL_OK}{booking_id}"
                ),
                InlineKeyboardButton(
                    get_text("btn_keep_booking"), callback_data=f"{CB_KEEP_BK}{booking_id}"
                ),
            ]
        ]
    )
    await update.effective_chat.send_message(prompt, reply_markup=keyboard)


async def _handle_cancel_reason_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str
) -> None:
    """Called by the text router once the user sends their cancel reason."""
    booking_id = context.user_data.pop(CANCEL_BK_KEY, None)
    context.user_data[AWAIT_KEY] = None
    if not booking_id:
        return
    # Persist the reason on the booking so admin/future-self can read it.
    storage.update_booking(booking_id, cancel_reason=reason)
    await update.message.reply_text(get_text("cancel_reason_saved"))
    await _show_cancel_confirm(update, booking_id)


async def do_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commit the cancellation using the policy (free / quota / forfeit)."""
    query = update.callback_query
    await query.answer()

    booking_id = int(query.data.removeprefix(CB_CANCEL_OK))
    booking = storage.get_booking(booking_id)
    if not booking or booking["user_id"] != update.effective_user.id:
        await query.edit_message_text(get_text("error_generic"))
        return
    if booking["status"] not in ACTIONABLE_STATUSES:
        await query.edit_message_text(get_text("cancel_kept"))
        return

    slot = storage.get_slot(booking["slot_id"]) or {}
    slot_str = slot.get("datetime", "")
    slot_human = format_slot_human(slot_str) if slot_str else "—"
    hours_left = _hours_until_slot(slot_str) if slot_str else 0.0
    user_id = booking["user_id"]
    reason = booking.get("cancel_reason", "—")

    storage.update_booking(booking_id, status="cancelled")
    storage.mark_slot_booked(booking["slot_id"], booked=False)
    storage.increment_sessions_cancelled(user_id)

    if hours_left >= CANCEL_FREE_HOURS:
        storage.add_available_session(user_id, 1)
        reply = get_text("cancel_done_reusable")
        policy = "free-reusable"
    else:
        free_left = storage.refresh_free_cancellations(user_id)
        if free_left > 0:
            new_left = storage.consume_free_cancellation(user_id)
            storage.add_available_session(user_id, 1)
            reply = get_text("cancel_done_quota", left=new_left)
            policy = "quota-covered"
        else:
            reply = get_text("cancel_done_lost")
            policy = "forfeit"

    logger.info(
        "booking cancelled id=%s user=%s policy=%s hours_left=%.1f reason=%r",
        booking_id, user_id, policy, hours_left, reason,
    )

    # Clean up scheduled reminder / session-end jobs for this booking.
    for name in (f"reminder:{booking_id}", f"session_end:{booking_id}"):
        for job in context.application.job_queue.get_jobs_by_name(name):
            job.schedule_removal()

    await query.edit_message_text(reply)

    user = storage.get_user(user_id) or {}
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=get_text(
                "admin_cancel_notice",
                name=user.get("name", "—"),
                user_id=user_id,
                slot=slot_human,
                reason=f"{policy} | {reason}",
            ),
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to notify admin about cancellation")


async def keep_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # User changed their mind — clear any pending cancel reason state.
    context.user_data[AWAIT_KEY] = None
    context.user_data.pop(CANCEL_BK_KEY, None)
    await query.edit_message_text(get_text("cancel_kept"))


# ---------------------------------------------------------------------------
# Reschedule flow — if not allowed, always expose a fallback Cancel button
# ---------------------------------------------------------------------------

async def ask_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    booking_id = int(query.data.removeprefix(CB_RESCH))
    booking = storage.get_booking(booking_id)
    if not booking or booking["user_id"] != update.effective_user.id:
        await query.edit_message_text(get_text("error_generic"))
        return
    if booking["status"] not in ACTIONABLE_STATUSES:
        await query.edit_message_text(get_text("cancel_kept"))
        return

    slot = storage.get_slot(booking["slot_id"]) or {}
    if _hours_until_slot(slot.get("datetime", "")) < CANCEL_FREE_HOURS:
        # Reschedule is off the table, but the user must still be able to
        # cancel — expose Cancel + Back buttons alongside the explanation.
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        get_text("btn_cancel_booking"),
                        callback_data=f"{CB_CANCEL_BK}{booking_id}",
                    )
                ],
                back_button_row(),
            ]
        )
        await query.edit_message_text(get_text("reschedule_not_allowed"), reply_markup=keyboard)
        return

    free = [s for s in storage.list_free_slots() if s["id"] != booking["slot_id"]]
    if not free:
        await query.edit_message_text(get_text("reschedule_no_slots"))
        return

    keyboard = [
        [
            InlineKeyboardButton(
                format_slot_human(s["datetime"]),
                callback_data=f"{CB_RESCH_PICK}{booking_id}:{s['id']}",
            )
        ]
        for s in free
    ]
    keyboard.append(back_button_row())
    await query.edit_message_text(
        get_text("reschedule_choose_slot"),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def do_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.reminder import schedule_reminder, schedule_session_end

    query = update.callback_query
    await query.answer()

    payload = query.data.removeprefix(CB_RESCH_PICK)
    try:
        old_bk_id_s, new_slot_id_s = payload.split(":")
        old_bk_id = int(old_bk_id_s)
        new_slot_id = int(new_slot_id_s)
    except ValueError:
        await query.edit_message_text(get_text("error_generic"))
        return

    booking = storage.get_booking(old_bk_id)
    if not booking or booking["user_id"] != update.effective_user.id:
        await query.edit_message_text(get_text("error_generic"))
        return
    if booking["status"] not in ACTIONABLE_STATUSES:
        await query.edit_message_text(get_text("cancel_kept"))
        return

    old_slot = storage.get_slot(booking["slot_id"]) or {}
    if _hours_until_slot(old_slot.get("datetime", "")) < CANCEL_FREE_HOURS:
        await query.edit_message_text(get_text("reschedule_not_allowed"))
        return

    if not storage.try_book_slot(new_slot_id):
        await query.edit_message_text(get_text("slot_taken"))
        return

    storage.mark_slot_booked(booking["slot_id"], booked=False)
    storage.update_booking(old_bk_id, slot_id=new_slot_id)

    for name in (f"reminder:{old_bk_id}", f"session_end:{old_bk_id}"):
        for job in context.application.job_queue.get_jobs_by_name(name):
            job.schedule_removal()

    new_slot = storage.get_slot(new_slot_id) or {}
    new_slot_str = new_slot.get("datetime", "")
    schedule_reminder(context.application, booking["user_id"], old_bk_id, new_slot_str)
    schedule_session_end(context.application, booking["user_id"], old_bk_id, new_slot_str)

    logger.info("booking rescheduled id=%s new_slot=%s", old_bk_id, new_slot_str)
    await query.edit_message_text(get_text("reschedule_done", slot=format_slot_human(new_slot_str)))


# ---------------------------------------------------------------------------
# Back button — returns user to the main menu (reply keyboard)
# ---------------------------------------------------------------------------

async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # Clear any "awaiting" state the user might have been in.
    context.user_data[AWAIT_KEY] = None
    # Replace the inline message with a neutral line so old buttons disappear.
    try:
        await query.edit_message_text(get_text("back_to_menu"))
    except Exception:  # noqa: BLE001 — editing old messages can fail harmlessly
        pass
    # The persistent ReplyKeyboard is already visible, but re-sending it
    # guarantees it's restored even if the client dropped it.
    await update.effective_chat.send_message(get_text("menu_title"), reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Text router — the single MessageHandler(filters.TEXT & ~filters.COMMAND)
# outside ConversationHandler. Handles:
#   1. Main-menu reply-keyboard buttons.
#   2. Awaited text input (cancel reason, payment comment).
# ---------------------------------------------------------------------------

async def _forward_client_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Forward a client's free-form message to the admin.

    During working hours (09–19 Kyiv): auto-reply + forward.
    Outside hours: auto-reply only, NO admin notification.
    """
    from config import WORKING_HOURS_END, WORKING_HOURS_START

    now_kyiv = datetime.now(tz=KYIV_OFFSET)
    in_hours = WORKING_HOURS_START <= now_kyiv.hour < WORKING_HOURS_END

    if in_hours:
        await update.message.reply_text(
            get_text("client_msg_working"), reply_markup=main_menu_keyboard()
        )
        user = storage.get_user(update.effective_user.id) or {}
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                get_text("admin_btn_reply"),
                callback_data=f"admin_reply:{update.effective_user.id}",
            )]]
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=get_text(
                    "admin_client_msg",
                    name=user.get("name", "—"),
                    username=user.get("tg_username") or "—",
                    text=text,
                ),
                reply_markup=keyboard,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to forward client msg to admin")
    else:
        await update.message.reply_text(
            get_text("client_msg_after_hours"), reply_markup=main_menu_keyboard()
        )


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.admin import handle_text_if_admin_awaiting
    from handlers.payment import (
        AWAIT_PAYMENT_COMMENT,
        finalize_payment_comment,
    )

    # Guard: if user hasn't finished the questionnaire, they belong to the
    # ConversationHandler. If we land here it means conversation state was
    # lost (e.g. bot restart mid-anketa). Silently ignore — the user will
    # need to type /start to re-enter the questionnaire.
    uid = update.effective_user.id
    u = storage.get_user(uid)
    if u and not u.get("questionnaire_done"):
        return

    text = (update.message.text or "").strip()
    labels = menu_labels()

    # (1) Main-menu button pressed — always wins, clears awaiting flags.
    if text in labels:
        context.user_data[AWAIT_KEY] = None
        if text == get_text("menu_book"):
            await show_slots(update, context)
        elif text == get_text("menu_my_bookings"):
            await my_bookings(update, context)
        elif text == get_text("menu_profile"):
            from handlers.start import profile
            await profile(update, context)
        elif text == get_text("menu_rules"):
            await update.message.reply_text(get_text("rules_menu"), reply_markup=main_menu_keyboard())
        return

    # (2) Admin-side awaiting (msg-to-client, range-add conversation).
    if await handle_text_if_admin_awaiting(update, context):
        return

    # (3) User-side awaiting inputs
    awaiting = context.user_data.get(AWAIT_KEY)
    if awaiting == AWAIT_CANCEL_REASON:
        await _handle_cancel_reason_input(update, context, text)
        return
    if awaiting == AWAIT_PAYMENT_COMMENT:
        await finalize_payment_comment(update, context, text)
        return

    # (4) Free-form text outside any active flow → forward to admin as a
    #     client message (respecting working hours).
    if text and storage.user_exists(update.effective_user.id):
        await _forward_client_message(update, context, text)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(app) -> None:
    app.add_handler(CommandHandler("book", show_slots))
    app.add_handler(CommandHandler("my", my_bookings))

    # Booking flow callbacks
    app.add_handler(CallbackQueryHandler(on_slot_chosen, pattern=f"^{CB_SLOT}"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=f"^{CB_CONFIRM}"))
    app.add_handler(CallbackQueryHandler(on_slot_dialog_cancel, pattern=f"^{CB_CANCEL_DIALOG}$"))

    # Cancel / reschedule (longer prefixes must come first so they match).
    app.add_handler(CallbackQueryHandler(do_cancel, pattern=f"^{CB_CANCEL_OK}"))
    app.add_handler(CallbackQueryHandler(ask_cancel_reason, pattern=f"^{CB_CANCEL_BK}"))
    app.add_handler(CallbackQueryHandler(keep_booking, pattern=f"^{CB_KEEP_BK}"))
    app.add_handler(CallbackQueryHandler(do_reschedule, pattern=f"^{CB_RESCH_PICK}"))
    app.add_handler(CallbackQueryHandler(ask_reschedule, pattern=f"^{CB_RESCH}"))

    # Back to menu
    app.add_handler(CallbackQueryHandler(on_back, pattern=f"^{CB_BACK}$"))

    # Text router — handles reply-keyboard buttons + awaited free-form inputs.
    # Must be last so it doesn't shadow command or callback handlers.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))