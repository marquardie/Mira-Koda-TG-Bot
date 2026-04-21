"""/start command, client-type picker, and the questionnaire."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from handlers.common import format_slot_human, main_menu_keyboard, status_text
from services import storage
from services.texts import get_text

# Conversation states — Q_CLIENT_TYPE is the new first step (inline buttons),
# Q_MEDICATION was merged into Q_DIAGNOSIS so the constant is recycled.
Q_CLIENT_TYPE, Q_NAME, Q_AGE, Q_REQUEST, Q_EXPERIENCE, Q_DIAGNOSIS = range(6)

CB_NEW_CLIENT = "ct:new"
CB_RETURNING_CLIENT = "ct:ret"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: returning users see the menu; new users pick client type."""
    user = update.effective_user
    if storage.user_exists(user.id):
        profile_data = storage.get_user(user.id) or {}
        await update.message.reply_text(
            get_text("welcome_returning", name=profile_data.get("name", user.first_name or "")),
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    storage.save_user(user.id, {"tg_username": user.username or "", "questionnaire_done": False})
    await update.message.reply_text(get_text("welcome_new"))
    await update.message.reply_text(
        get_text("client_type_prompt"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(get_text("btn_new_client"), callback_data=CB_NEW_CLIENT)],
            [InlineKeyboardButton(get_text("btn_returning_client"), callback_data=CB_RETURNING_CLIENT)],
        ]),
    )
    return Q_CLIENT_TYPE


async def on_client_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picks 'new' or 'returning'. Both paths lead to the questionnaire.

    Also registered as an entry_point so it works even when the in-memory
    conversation state was lost after a server restart.
    """
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    # Guard: if bot restarted before user record was saved, create it now.
    if not storage.get_user(uid):
        storage.save_user(uid, {"tg_username": update.effective_user.username or "", "questionnaire_done": False})
    client_type = "returning" if query.data == CB_RETURNING_CLIENT else "new"
    storage.save_user(uid, {"client_type": client_type})
    await query.edit_message_text(get_text("q_name"))
    return Q_NAME


async def q_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    storage.save_user(update.effective_user.id, {"name": update.message.text.strip()})
    await update.message.reply_text(get_text("q_age"))
    return Q_AGE


async def q_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 120):
        await update.message.reply_text(get_text("q_age_invalid"))
        return Q_AGE
    storage.save_user(update.effective_user.id, {"age": int(text)})
    await update.message.reply_text(get_text("q_request"))
    return Q_REQUEST


async def q_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    storage.save_user(update.effective_user.id, {"request": update.message.text.strip()})
    await update.message.reply_text(get_text("q_experience"))
    return Q_EXPERIENCE


async def q_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    storage.save_user(update.effective_user.id, {"experience": update.message.text.strip()})
    await update.message.reply_text(get_text("q_diagnosis"))
    return Q_DIAGNOSIS


async def q_diagnosis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Combined diagnosis + medication question (was two separate steps)."""
    user_id = update.effective_user.id
    storage.save_user(
        user_id,
        {
            "diagnosis": update.message.text.strip(),
            "medication": "—",
            "questionnaire_done": True,
            "sessions_completed": 0,
            "sessions_cancelled": 0,
            "free_cancellations_left": storage.FREE_CANCELLATIONS_MAX,
            "available_sessions": 0,
            "last_payment_date": None,
            "package_offered": False,
        },
    )
    await update.message.reply_text(get_text("q_saved"), reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(get_text("cancelled"), reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def already_in_anketa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(get_text("anketa_in_progress"))


def build_start_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(on_client_type, pattern=r"^ct:"),
        ],
        states={
            Q_CLIENT_TYPE: [CallbackQueryHandler(on_client_type, pattern=r"^ct:")],
            Q_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_name)],
            Q_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_age)],
            Q_REQUEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_request)],
            Q_EXPERIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_experience)],
            Q_DIAGNOSIS: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_diagnosis)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", already_in_anketa),
        ],
        allow_reentry=False,
        per_chat=True,
        per_user=True,
    )


# ---------------------------------------------------------------------------
# Profile / help
# ---------------------------------------------------------------------------

ACTIVE_STATUSES = {"pending_payment", "waiting_confirm", "confirmed"}


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = storage.get_user_with_defaults(user_id)
    if not user or not user.get("questionnaire_done"):
        await update.message.reply_text(get_text("welcome_new"))
        return

    if user.get("package_active"):
        package_status = get_text("package_status_active", left=user.get("available_sessions", 0))
    else:
        package_status = get_text("package_status_inactive")

    text = get_text(
        "profile_header",
        name=user.get("name", "—"),
        age=user.get("age", "—"),
        request=user.get("request", "—"),
        experience=user.get("experience", "—"),
        diagnosis=user.get("diagnosis", "—"),
        sessions_completed=user.get("sessions_completed", 0),
        sessions_cancelled=user.get("sessions_cancelled", 0),
        free_cancellations_left=user.get("free_cancellations_left", storage.FREE_CANCELLATIONS_MAX),
        available_sessions=user.get("available_sessions", 0),
        package_status=package_status,
    )

    active = [
        b for b in storage.list_user_bookings(user_id)
        if b.get("status") in ACTIVE_STATUSES
    ]
    if active:
        text += get_text("profile_bookings_header")
        for b in sorted(active, key=lambda x: x["id"]):
            slot = storage.get_slot(b["slot_id"]) or {}
            text += "\n" + get_text(
                "profile_bookings_line",
                slot=format_slot_human(slot.get("datetime", "—")),
                status=status_text(b["status"]),
            )
    else:
        text += get_text("profile_bookings_empty")

    # Package CTA for eligible returning clients.
    from handlers.payment import CB_PKG_BUY
    markup = main_menu_keyboard()
    if (user.get("sessions_completed", 0) >= 3
            and not user.get("package_active")
            and user.get("client_type") == "returning"):
        await update.message.reply_text(text, reply_markup=markup)
        await update.message.reply_text(
            get_text("package_offer_card"),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(get_text("btn_buy_package"), callback_data=CB_PKG_BUY)]]
            ),
        )
        return
    await update.message.reply_text(text, reply_markup=markup)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(get_text("help_text"), reply_markup=main_menu_keyboard())