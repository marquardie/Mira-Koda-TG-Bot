"""Admin CLI commands + an interactive inline admin panel (``/admin``).

Architecture
------------
* ``open_admin_menu`` handles the ``/admin`` entry and shows the inline menu.
* ``handle_admin_callbacks`` is a single dispatcher registered against the
  ``^admin_`` pattern that routes every inline button press to its action.
* All existing CLI admin commands (/slots_add, /slots_list, /slots_del,
  /send, /bookings) keep working.

Callback-data grammar (all start with ``admin_``):
    admin_menu
    admin_clients
    admin_client:<user_id>
    admin_today
    admin_add_slots
    admin_add_one
    admin_add_range
    admin_message
    admin_msg_pick:<user_id>
    admin_stats
    admin_cancel:<booking_id>
    admin_resch:<booking_id>
    admin_resch_pick:<booking_id>:<slot_id>
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from config import ADMIN_ID
from services import storage
from services.texts import get_text

logger = logging.getLogger(__name__)

KYIV_OFFSET = timezone(timedelta(hours=3))
ACTIONABLE_STATUSES = {"pending_payment", "waiting_confirm", "confirmed"}

# Callback-data constants (all start with "admin_" so a single
# CallbackQueryHandler can match them via the pattern "^admin_").
CB_MENU = "admin_menu"
CB_CLIENTS = "admin_clients"
CB_CLIENT = "admin_client:"          # admin_client:<user_id>
CB_TODAY = "admin_today"
CB_ADD_SLOTS = "admin_add_slots"
CB_ADD_ONE = "admin_add_one"
CB_ADD_RANGE = "admin_add_range"
CB_MESSAGE = "admin_message"
CB_MSG_PICK = "admin_msg_pick:"      # admin_msg_pick:<user_id>
CB_STATS = "admin_stats"
CB_CANCEL = "admin_cancel:"          # admin_cancel:<booking_id>
CB_RESCH = "admin_resch:"            # admin_resch:<booking_id>
CB_RESCH_PICK = "admin_resch_pick:"  # admin_resch_pick:<bk_id>:<slot_id>

# Delete client / slot
CB_DEL_CLIENTS = "admin_del_clients"
CB_DEL_CLIENT = "admin_del_client:"        # admin_del_client:<user_id>
CB_DEL_CONFIRM = "admin_del_confirm:"      # admin_del_confirm:<user_id>
CB_DEL_SLOTS = "admin_del_slots"
CB_DEL_SLOT = "admin_del_slot:"            # admin_del_slot:<slot_id>
CB_DEL_SLOT_OK = "admin_del_slot_ok:"      # admin_del_slot_ok:<slot_id>

# Calendar sub-menu
CB_CALENDAR = "admin_calendar"
CB_CAL_TODAY = "admin_cal_today"
CB_CAL_TOMORROW = "admin_cal_tomorrow"
CB_CAL_THIS_WEEK = "admin_cal_this_week"
CB_CAL_NEXT_WEEK = "admin_cal_next_week"
CB_SCHEDULE = "admin_schedule"
CB_SCHEDULE_OFF = "admin_schedule_off:"  # admin_schedule_off:<offset>
CB_DAY = "admin_day:"                    # admin_day:YYYY-MM-DD

# Reply to client message
CB_REPLY = "admin_reply:"               # admin_reply:<user_id>

# context.user_data keys for the text-input driven flows
AWAIT_KEY = "awaiting"
AWAIT_MSG_TEXT = "admin_msg_text"
AWAIT_REPLY_TEXT = "admin_reply_text"
AWAIT_RANGE_DATE = "admin_range_date"
AWAIT_RANGE_START = "admin_range_start"
AWAIT_RANGE_END = "admin_range_end"
AWAIT_RANGE_STEP = "admin_range_step"


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def _is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


async def _reject_non_admin_msg(update: Update) -> bool:
    if not _is_admin(update):
        await update.message.reply_text(get_text("admin_only"))
        return True
    return False


async def _reject_non_admin_cb(update: Update) -> bool:
    if not _is_admin(update):
        await update.callback_query.answer(get_text("admin_only"), show_alert=True)
        return True
    return False


# ---------------------------------------------------------------------------
# CLI commands (unchanged — still available as shortcuts)
# ---------------------------------------------------------------------------

async def slots_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.common import format_slot_human

    if await _reject_non_admin_msg(update):
        return
    if not context.args:
        await update.message.reply_text(get_text("slot_format_error"))
        return
    dt = storage.parse_admin_slot_input(" ".join(context.args))
    if dt is None:
        await update.message.reply_text(get_text("slot_format_error"))
        return
    slot = storage.add_slot(storage.to_storage_format(dt))
    await update.message.reply_text(get_text("slot_added", slot=format_slot_human(slot["datetime"])))


async def slots_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_non_admin_msg(update):
        return
    slots = storage.list_slots()
    if not slots:
        await update.message.reply_text(get_text("slots_list_empty"))
        return
    lines = [get_text("slots_list_header")]
    for s in slots:
        status = get_text("slot_status_busy") if s["booked"] else get_text("slot_status_free")
        lines.append(get_text("slots_list_line", id=s["id"], slot=s["datetime"], status=status))
    await update.message.reply_text("\n".join(lines))


async def slots_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_non_admin_msg(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text(get_text("slot_format_error"))
        return
    if storage.delete_slot(int(context.args[0])):
        await update.message.reply_text(get_text("slot_deleted"))
    else:
        await update.message.reply_text(get_text("slot_not_found"))


async def send_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_non_admin_msg(update):
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text(get_text("send_format_error"))
        return
    target_id = int(context.args[0])
    body = " ".join(context.args[1:])
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=get_text("admin_message_prefix") + body,
        )
        await update.message.reply_text(get_text("send_ok"))
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(get_text("send_failed", error=str(exc)))


async def bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_non_admin_msg(update):
        return
    items = storage.list_all_bookings()
    if not items:
        await update.message.reply_text(get_text("bookings_empty"))
        return
    lines = [get_text("bookings_header")]
    for b in items:
        slot = storage.get_slot(b["slot_id"]) or {}
        user = storage.get_user(b["user_id"]) or {}
        lines.append(
            get_text(
                "bookings_line",
                id=b["id"], slot=slot.get("datetime", "—"),
                name=user.get("name", "—"), user_id=b["user_id"], status=b["status"],
            )
        )
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Inline keyboards
# ---------------------------------------------------------------------------

def _panel_keyboard() -> InlineKeyboardMarkup:
    """Main admin panel."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(get_text("admin_menu_clients"), callback_data=CB_CLIENTS)],
            [InlineKeyboardButton(get_text("admin_menu_calendar"), callback_data=CB_CALENDAR)],
            [InlineKeyboardButton(get_text("admin_menu_add_slots"), callback_data=CB_ADD_SLOTS)],
            [InlineKeyboardButton(get_text("admin_menu_msg"), callback_data=CB_MESSAGE)],
            [InlineKeyboardButton(get_text("admin_menu_stats"), callback_data=CB_STATS)],
            [InlineKeyboardButton(get_text("admin_menu_del_client"), callback_data=CB_DEL_CLIENTS)],
        ]
    )


def _back_row(target: str = CB_MENU) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(get_text("admin_btn_back_panel"), callback_data=target)]


# ---------------------------------------------------------------------------
# /admin entry point
# ---------------------------------------------------------------------------

async def open_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the inline admin panel. Only reachable by ADMIN_ID."""
    if await _reject_non_admin_msg(update):
        return
    await update.message.reply_text(
        get_text("admin_panel_title"), reply_markup=_panel_keyboard()
    )


# ---------------------------------------------------------------------------
# Single callback dispatcher — handles every ``admin_*`` inline button
# ---------------------------------------------------------------------------

async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route every admin-panel callback to the appropriate section."""
    if await _reject_non_admin_cb(update):
        return
    q = update.callback_query
    data = q.data or ""
    logger.info("admin callback user=%s data=%s", update.effective_user.id, data)

    # Back to the main panel.
    if data == CB_MENU:
        await q.answer()
        # Clear any half-entered admin awaiting state.
        for k in (AWAIT_KEY, "admin_msg_user_id",
                  "admin_range_date", "admin_range_start", "admin_range_end"):
            context.user_data.pop(k, None)
        try:
            await q.edit_message_text(get_text("admin_panel_title"), reply_markup=_panel_keyboard())
        except Exception:  # noqa: BLE001
            await q.message.reply_text(get_text("admin_panel_title"), reply_markup=_panel_keyboard())
        return

    if data == CB_CLIENTS:
        await q.answer()
        await _render_clients(update)
        return

    if data.startswith(CB_CLIENT):
        await q.answer()
        await _render_client(update, int(data.removeprefix(CB_CLIENT)))
        return

    if data == CB_TODAY:
        await q.answer()
        await _render_today(update)
        return

    if data == CB_ADD_SLOTS:
        await q.answer()
        await _render_add_slots(update)
        return

    if data == CB_ADD_ONE:
        await q.answer()
        await _render_add_one(update)
        return

    if data == CB_ADD_RANGE:
        await q.answer()
        await _start_range(update, context)
        return

    if data == CB_MESSAGE:
        await q.answer()
        await _render_message_menu(update)
        return

    if data.startswith(CB_MSG_PICK):
        await q.answer()
        await _start_message_to_client(update, context, int(data.removeprefix(CB_MSG_PICK)))
        return

    if data == CB_STATS:
        await q.answer()
        await _render_stats(update)
        return

    if data.startswith(CB_CANCEL):
        await q.answer()
        await _do_admin_cancel(update, context, int(data.removeprefix(CB_CANCEL)))
        return

    if data.startswith(CB_RESCH_PICK):
        await q.answer()
        payload = data.removeprefix(CB_RESCH_PICK)
        try:
            bk_s, slot_s = payload.split(":")
            await _do_admin_reschedule(update, context, int(bk_s), int(slot_s))
        except ValueError:
            await q.edit_message_text(get_text("error_generic"))
        return

    if data.startswith(CB_RESCH):
        await q.answer()
        await _start_admin_reschedule(update, int(data.removeprefix(CB_RESCH)))
        return

    # ---- Calendar sub-menu ----
    if data == CB_CALENDAR:
        await q.answer()
        await _render_calendar_menu(update)
        return

    if data == CB_CAL_TODAY:
        await q.answer()
        await _render_day_bookings(update, datetime.now(tz=KYIV_OFFSET).date())
        return

    if data == CB_CAL_TOMORROW:
        await q.answer()
        await _render_day_bookings(update, datetime.now(tz=KYIV_OFFSET).date() + timedelta(days=1))
        return

    if data == CB_CAL_THIS_WEEK:
        await q.answer()
        await _render_week(update, offset=0)
        return

    if data == CB_CAL_NEXT_WEEK:
        await q.answer()
        await _render_week(update, offset=1)
        return

    if data == CB_SCHEDULE or data.startswith(CB_SCHEDULE_OFF):
        await q.answer()
        off = 0
        if data.startswith(CB_SCHEDULE_OFF):
            try:
                off = int(data.removeprefix(CB_SCHEDULE_OFF))
            except ValueError:
                pass
        await _render_schedule_grid(update, off)
        return

    if data.startswith(CB_DAY):
        await q.answer()
        day_str = data.removeprefix(CB_DAY)
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            await q.edit_message_text(get_text("error_generic"))
            return
        await _render_day_bookings(update, day)
        return

    # ---- Reply to client message ----
    if data.startswith(CB_REPLY):
        await q.answer()
        try:
            uid = int(data.removeprefix(CB_REPLY))
        except ValueError:
            return
        context.user_data[AWAIT_KEY] = AWAIT_REPLY_TEXT
        context.user_data["admin_reply_user_id"] = uid
        await q.edit_message_text(get_text("admin_reply_ask_text"))
        return

    # ---- Delete client ----
    if data == CB_DEL_CLIENTS:
        await q.answer()
        await _render_del_clients(update)
        return

    if data.startswith(CB_DEL_CONFIRM):
        await q.answer()
        await _do_delete_user(update, int(data.removeprefix(CB_DEL_CONFIRM)))
        return

    if data.startswith(CB_DEL_CLIENT):
        await q.answer()
        await _confirm_delete_user(update, int(data.removeprefix(CB_DEL_CLIENT)))
        return

    # ---- Delete slot ----
    if data == CB_DEL_SLOTS:
        await q.answer()
        await _render_del_slots(update)
        return

    if data.startswith(CB_DEL_SLOT_OK):
        await q.answer()
        await _do_delete_slot(update, int(data.removeprefix(CB_DEL_SLOT_OK)))
        return

    if data.startswith(CB_DEL_SLOT):
        await q.answer()
        await _confirm_delete_slot(update, int(data.removeprefix(CB_DEL_SLOT)))
        return

    # Legacy admin_today (backward compat for old messages)
    if data == CB_TODAY:
        await q.answer()
        await _render_day_bookings(update, datetime.now(tz=KYIV_OFFSET).date())
        return

    # Unknown admin_ callback — just ack so the spinner stops.
    await q.answer()


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _all_clients() -> list[tuple[int, dict]]:
    """All users who finished the questionnaire, sorted by name."""
    from services.storage import _read_json
    from config import USERS_FILE
    data = _read_json(USERS_FILE)
    out = [(int(uid), u) for uid, u in data.items() if u.get("questionnaire_done")]
    out.sort(key=lambda x: (x[1].get("name") or "").lower())
    return out


async def _render_clients(update: Update) -> None:
    q = update.callback_query
    clients = _all_clients()
    if not clients:
        await q.edit_message_text(
            get_text("admin_clients_empty"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )
        return
    lines = [get_text("admin_clients_header", count=len(clients))]
    keyboard = []
    for uid, u in clients:
        sessions = u.get("sessions_completed", u.get("sessions_count", 0))
        lines.append(get_text(
            "admin_clients_line",
            name=u.get("name", "—"),
            username=u.get("tg_username") or "—",
            sessions=sessions,
        ))
        label = (u.get("name") or f"id {uid}")[:40]
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{CB_CLIENT}{uid}")])
    keyboard.append(_back_row())
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


def _future_bookings_for(user_id: int) -> list[dict]:
    now = datetime.now(tz=KYIV_OFFSET)
    out = []
    for b in storage.list_user_bookings(user_id):
        if b.get("status") not in ACTIONABLE_STATUSES:
            continue
        slot = storage.get_slot(b["slot_id"]) or {}
        naive = storage.parse_slot(slot.get("datetime", ""))
        if not naive or naive.replace(tzinfo=KYIV_OFFSET) <= now:
            continue
        out.append({**b, "_slot": slot})
    out.sort(key=lambda x: x["_slot"].get("datetime", ""))
    return out


async def _render_client(update: Update, user_id: int) -> None:
    from handlers.common import format_slot_human

    q = update.callback_query
    u = storage.get_user_with_defaults(user_id)
    if not u:
        await q.edit_message_text(
            get_text("error_generic"),
            reply_markup=InlineKeyboardMarkup([_back_row(CB_CLIENTS)]),
        )
        return

    text = get_text(
        "admin_client_profile",
        name=u.get("name", "—"),
        age=u.get("age", "—"),
        request=u.get("request", "—"),
        experience=u.get("experience", "—"),
        diagnosis=u.get("diagnosis", "—"),
        sessions_completed=u.get("sessions_completed", 0),
        available_sessions=u.get("available_sessions", 0),
        sessions_cancelled=u.get("sessions_cancelled", 0),
        free_cancellations_left=u.get("free_cancellations_left", storage.FREE_CANCELLATIONS_MAX),
    )

    future = _future_bookings_for(user_id)
    if future:
        text += get_text("admin_client_future_header")
        for b in future:
            text += f"\n• #{b['id']} {format_slot_human(b['_slot']['datetime'])}"
    else:
        text += get_text("admin_client_future_empty")

    rows = [[InlineKeyboardButton(
        get_text("admin_btn_msg_client"), callback_data=f"{CB_MSG_PICK}{user_id}"
    )]]
    for b in future:
        rows.append([
            InlineKeyboardButton(get_text("admin_btn_resch_bk"),
                                 callback_data=f"{CB_RESCH}{b['id']}"),
            InlineKeyboardButton(get_text("admin_btn_cancel_bk"),
                                 callback_data=f"{CB_CANCEL}{b['id']}"),
        ])
    rows.append(_back_row(CB_CLIENTS))
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def _render_today(update: Update) -> None:
    q = update.callback_query
    today = datetime.now(tz=KYIV_OFFSET).date()
    rows_data = []
    for b in storage.list_all_bookings():
        if b.get("status") not in ACTIONABLE_STATUSES:
            continue
        slot = storage.get_slot(b["slot_id"]) or {}
        dt = storage.parse_slot(slot.get("datetime", ""))
        if not dt or dt.date() != today:
            continue
        user = storage.get_user(b["user_id"]) or {}
        rows_data.append((dt, user.get("name", "—"), b["user_id"]))
    rows_data.sort(key=lambda r: r[0])

    if not rows_data:
        await q.edit_message_text(
            get_text("admin_today_empty"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )
        return

    lines = [get_text("admin_today_header", date=today.strftime("%d.%m.%Y"))]
    keyboard = []
    for dt, name, uid in rows_data:
        time_str = dt.strftime("%H:%M")
        lines.append(get_text("admin_today_line", time=time_str, name=name))
        keyboard.append([InlineKeyboardButton(
            f"{time_str} — {name}"[:40], callback_data=f"{CB_CLIENT}{uid}"
        )])
    keyboard.append(_back_row())
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def _render_add_slots(update: Update) -> None:
    q = update.callback_query
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(get_text("admin_btn_add_one"), callback_data=CB_ADD_ONE)],
            [InlineKeyboardButton(get_text("admin_btn_add_range"), callback_data=CB_ADD_RANGE)],
            [InlineKeyboardButton(get_text("admin_btn_del_slots"), callback_data=CB_DEL_SLOTS)],
            _back_row(),
        ]
    )
    await q.edit_message_text(get_text("admin_add_slots_menu"), reply_markup=keyboard)


async def _render_add_one(update: Update) -> None:
    q = update.callback_query
    await q.edit_message_text(
        get_text("admin_add_one_hint"),
        reply_markup=InlineKeyboardMarkup([_back_row()]),
    )


async def _start_range(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    context.user_data[AWAIT_KEY] = AWAIT_RANGE_DATE
    for k in ("admin_range_date", "admin_range_start", "admin_range_end"):
        context.user_data.pop(k, None)
    await q.edit_message_text(get_text("admin_range_ask_date"))


async def _render_message_menu(update: Update) -> None:
    q = update.callback_query
    clients = _all_clients()
    if not clients:
        await q.edit_message_text(
            get_text("admin_msg_clients_empty"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )
        return
    keyboard = []
    for uid, u in clients:
        label = (u.get("name") or f"id {uid}")[:40]
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{CB_MSG_PICK}{uid}")])
    keyboard.append(_back_row())
    await q.edit_message_text(get_text("admin_msg_pick_client"),
                              reply_markup=InlineKeyboardMarkup(keyboard))


async def _start_message_to_client(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    q = update.callback_query
    user = storage.get_user(user_id) or {}
    context.user_data[AWAIT_KEY] = AWAIT_MSG_TEXT
    context.user_data["admin_msg_user_id"] = user_id
    await q.edit_message_text(
        get_text("admin_msg_ask_text", name=user.get("name", f"id {user_id}"))
    )


async def _render_stats(update: Update) -> None:
    q = update.callback_query
    clients = _all_clients()
    total_sessions = sum(
        u.get("sessions_completed", u.get("sessions_count", 0)) for _, u in clients
    )
    total_cancels = sum(u.get("sessions_cancelled", 0) for _, u in clients)
    await q.edit_message_text(
        get_text("admin_stats", users=len(clients),
                 sessions=total_sessions, cancels=total_cancels),
        reply_markup=InlineKeyboardMarkup([_back_row()]),
    )


# ---------------------------------------------------------------------------
# Admin cancel / reschedule
# ---------------------------------------------------------------------------

async def _do_admin_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE, booking_id: int
) -> None:
    q = update.callback_query
    booking = storage.get_booking(booking_id)
    if not booking or booking["status"] not in ACTIONABLE_STATUSES:
        await q.edit_message_text(
            get_text("error_generic"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )
        return

    storage.update_booking(booking_id, status="cancelled")
    storage.mark_slot_booked(booking["slot_id"], booked=False)
    # Admin-initiated cancel: always credit the client so they can re-book.
    storage.add_available_session(booking["user_id"], 1)

    for name in (f"reminder:{booking_id}", f"session_end:{booking_id}"):
        for job in context.application.job_queue.get_jobs_by_name(name):
            job.schedule_removal()

    logger.info("admin cancelled booking=%s user=%s", booking_id, booking["user_id"])
    try:
        await context.bot.send_message(
            chat_id=booking["user_id"], text=get_text("admin_cancel_to_client")
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to notify client about admin-cancel")

    await q.edit_message_text(
        get_text("admin_cancel_success", id=booking_id),
        reply_markup=InlineKeyboardMarkup([_back_row()]),
    )


async def _start_admin_reschedule(update: Update, booking_id: int) -> None:
    from handlers.common import format_slot_human

    q = update.callback_query
    booking = storage.get_booking(booking_id)
    if not booking or booking["status"] not in ACTIONABLE_STATUSES:
        await q.edit_message_text(
            get_text("error_generic"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )
        return

    free = [s for s in storage.list_free_slots() if s["id"] != booking["slot_id"]]
    if not free:
        await q.edit_message_text(
            get_text("admin_resch_no_slots"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )
        return

    keyboard = [
        [InlineKeyboardButton(
            format_slot_human(s["datetime"]),
            callback_data=f"{CB_RESCH_PICK}{booking_id}:{s['id']}",
        )]
        for s in free
    ]
    keyboard.append(_back_row())
    await q.edit_message_text(
        get_text("admin_resch_choose_slot", id=booking_id),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _do_admin_reschedule(
    update: Update, context: ContextTypes.DEFAULT_TYPE, booking_id: int, new_slot_id: int
) -> None:
    from handlers.common import format_slot_human
    from services.reminder import schedule_reminder, schedule_session_end

    q = update.callback_query
    booking = storage.get_booking(booking_id)
    if not booking or booking["status"] not in ACTIONABLE_STATUSES:
        await q.edit_message_text(get_text("error_generic"))
        return
    if not storage.try_book_slot(new_slot_id):
        await q.edit_message_text(get_text("slot_taken"))
        return

    storage.mark_slot_booked(booking["slot_id"], booked=False)
    storage.update_booking(booking_id, slot_id=new_slot_id)

    for name in (f"reminder:{booking_id}", f"session_end:{booking_id}"):
        for job in context.application.job_queue.get_jobs_by_name(name):
            job.schedule_removal()

    new_slot = storage.get_slot(new_slot_id) or {}
    new_slot_str = new_slot.get("datetime", "")
    schedule_reminder(context.application, booking["user_id"], booking_id, new_slot_str)
    schedule_session_end(context.application, booking["user_id"], booking_id, new_slot_str)

    slot_human = format_slot_human(new_slot_str)
    logger.info("admin rescheduled booking=%s → slot=%s", booking_id, new_slot_str)
    try:
        await context.bot.send_message(
            chat_id=booking["user_id"],
            text=get_text("admin_resch_to_client", slot=slot_human),
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to notify client about admin-reschedule")

    await q.edit_message_text(
        get_text("admin_resch_success", id=booking_id, slot=slot_human),
        reply_markup=InlineKeyboardMarkup([_back_row()]),
    )


# ---------------------------------------------------------------------------
# Delete client
# ---------------------------------------------------------------------------

async def _render_del_clients(update: Update) -> None:
    q = update.callback_query
    clients = _all_clients()
    if not clients:
        await q.edit_message_text(
            get_text("admin_clients_empty"), reply_markup=InlineKeyboardMarkup([_back_row()])
        )
        return
    kb = []
    for uid, u in clients:
        label = f"🗑 {u.get('name', f'id {uid}')}"[:40]
        kb.append([InlineKeyboardButton(label, callback_data=f"{CB_DEL_CLIENT}{uid}")])
    kb.append(_back_row())
    await q.edit_message_text(get_text("admin_del_clients_title"), reply_markup=InlineKeyboardMarkup(kb))


async def _confirm_delete_user(update: Update, user_id: int) -> None:
    q = update.callback_query
    user = storage.get_user(user_id) or {}
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_text("admin_btn_del_confirm"),
                                 callback_data=f"{CB_DEL_CONFIRM}{user_id}"),
            InlineKeyboardButton(get_text("admin_btn_del_cancel"), callback_data=CB_DEL_CLIENTS),
        ]
    ])
    await q.edit_message_text(
        get_text("admin_del_confirm_prompt",
                 name=user.get("name", "—"), user_id=user_id),
        reply_markup=kb,
    )


async def _do_delete_user(update: Update, user_id: int) -> None:
    q = update.callback_query
    if storage.delete_user(user_id):
        logger.info("admin deleted user=%s", user_id)
        await q.edit_message_text(
            get_text("admin_del_done"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )
    else:
        await q.edit_message_text(
            get_text("admin_del_not_found"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )


# ---------------------------------------------------------------------------
# Delete slot
# ---------------------------------------------------------------------------

async def _render_del_slots(update: Update) -> None:
    from handlers.common import format_slot_human

    q = update.callback_query
    slots = storage.list_slots()
    if not slots:
        await q.edit_message_text(
            get_text("slots_list_empty"), reply_markup=InlineKeyboardMarkup([_back_row(CB_ADD_SLOTS)])
        )
        return
    kb = []
    for s in slots:
        status = "🔴" if s.get("booked") else "🟢"
        label = f"{status} {format_slot_human(s['datetime'])}"[:40]
        kb.append([InlineKeyboardButton(label, callback_data=f"{CB_DEL_SLOT}{s['id']}")])
    kb.append(_back_row(CB_ADD_SLOTS))
    await q.edit_message_text(get_text("admin_del_slots_title"), reply_markup=InlineKeyboardMarkup(kb))


async def _confirm_delete_slot(update: Update, slot_id: int) -> None:
    from handlers.common import format_slot_human

    q = update.callback_query
    slot = storage.get_slot(slot_id)
    if not slot:
        await q.edit_message_text(
            get_text("slot_not_found"),
            reply_markup=InlineKeyboardMarkup([_back_row(CB_DEL_SLOTS)]),
        )
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_text("admin_btn_del_confirm"),
                                 callback_data=f"{CB_DEL_SLOT_OK}{slot_id}"),
            InlineKeyboardButton(get_text("admin_btn_del_cancel"), callback_data=CB_DEL_SLOTS),
        ]
    ])
    await q.edit_message_text(
        get_text("admin_del_slot_confirm", slot=format_slot_human(slot["datetime"])),
        reply_markup=kb,
    )


async def _do_delete_slot(update: Update, slot_id: int) -> None:
    q = update.callback_query
    if storage.delete_slot(slot_id):
        logger.info("admin deleted slot=%s", slot_id)
        await q.edit_message_text(
            get_text("admin_del_slot_done"),
            reply_markup=InlineKeyboardMarkup([_back_row(CB_ADD_SLOTS)]),
        )
    else:
        await q.edit_message_text(
            get_text("slot_not_found"),
            reply_markup=InlineKeyboardMarkup([_back_row(CB_ADD_SLOTS)]),
        )


# ---------------------------------------------------------------------------
# Calendar sub-menu
# ---------------------------------------------------------------------------

UA_DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


async def _render_calendar_menu(update: Update) -> None:
    q = update.callback_query
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(get_text("admin_cal_btn_today"), callback_data=CB_CAL_TODAY),
             InlineKeyboardButton(get_text("admin_cal_btn_tomorrow"), callback_data=CB_CAL_TOMORROW)],
            [InlineKeyboardButton(get_text("admin_cal_btn_this_week"), callback_data=CB_CAL_THIS_WEEK),
             InlineKeyboardButton(get_text("admin_cal_btn_next_week"), callback_data=CB_CAL_NEXT_WEEK)],
            [InlineKeyboardButton(get_text("admin_cal_btn_schedule"), callback_data=CB_SCHEDULE)],
            _back_row(),
        ]
    )
    await q.edit_message_text(get_text("admin_cal_menu_title"), reply_markup=kb)


def _bookings_for_date(day) -> list[tuple[datetime, str, int]]:
    """Return [(slot_dt, client_name, user_id)] for a given date."""
    rows = []
    for b in storage.list_all_bookings():
        if b.get("status") not in ACTIONABLE_STATUSES:
            continue
        slot = storage.get_slot(b["slot_id"]) or {}
        dt = storage.parse_slot(slot.get("datetime", ""))
        if not dt or dt.date() != day:
            continue
        user = storage.get_user(b["user_id"]) or {}
        rows.append((dt, user.get("name", "—"), b["user_id"]))
    rows.sort(key=lambda r: r[0])
    return rows


async def _render_day_bookings(update: Update, day) -> None:
    """Show bookings + free slots for a given day, each client clickable."""
    q = update.callback_query
    date_str = day.strftime("%d.%m.%Y")
    booked = _bookings_for_date(day)

    # Also show free (unbooked) slots for this day.
    free_slots = []
    for s in storage.list_slots():
        dt = storage.parse_slot(s.get("datetime", ""))
        if dt and dt.date() == day and not s.get("booked"):
            free_slots.append(dt)
    free_slots.sort()

    if not booked and not free_slots:
        await q.edit_message_text(
            get_text("admin_day_empty"),
            reply_markup=InlineKeyboardMarkup([_back_row(CB_CALENDAR)]),
        )
        return

    lines = [get_text("admin_day_header", date=date_str)]
    keyboard = []
    for dt, name, uid in booked:
        lines.append(get_text("admin_day_slot_booked", time=dt.strftime("%H:%M"), name=name))
        keyboard.append([InlineKeyboardButton(
            f"{dt.strftime('%H:%M')} — {name}"[:40], callback_data=f"{CB_CLIENT}{uid}"
        )])
    for dt in free_slots:
        lines.append(get_text("admin_day_slot_line",
                              time=dt.strftime("%H:%M"), status=get_text("slot_status_free")))

    # Day navigation: prev / next (limited to ±30 days from today).
    today = datetime.now(tz=KYIV_OFFSET).date()
    nav = []
    prev_day = day - timedelta(days=1)
    next_day = day + timedelta(days=1)
    if prev_day >= today - timedelta(days=7):
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{CB_DAY}{prev_day.strftime('%Y-%m-%d')}"))
    if next_day <= today + timedelta(days=30):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{CB_DAY}{next_day.strftime('%Y-%m-%d')}"))
    if nav:
        keyboard.append(nav)
    keyboard.append(_back_row(CB_CALENDAR))

    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def _render_week(update: Update, offset: int) -> None:
    """Show bookings for the week: current (offset=0) or next (offset=1)."""
    q = update.callback_query
    today = datetime.now(tz=KYIV_OFFSET).date()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    sunday = monday + timedelta(days=6)

    title = get_text("admin_cal_btn_this_week") if offset == 0 else get_text("admin_cal_btn_next_week")
    range_str = f"{monday.strftime('%d.%m')} — {sunday.strftime('%d.%m')}"

    all_rows = []
    for d in range(7):
        day = monday + timedelta(days=d)
        for dt, name, uid in _bookings_for_date(day):
            all_rows.append((day, dt, name, uid))

    if not all_rows:
        await q.edit_message_text(
            get_text("admin_week_empty"),
            reply_markup=InlineKeyboardMarkup([_back_row(CB_CALENDAR)]),
        )
        return

    lines = [get_text("admin_week_header", title=title, range=range_str)]
    keyboard = []
    for day, dt, name, uid in all_rows:
        label = f"{day.strftime('%d.%m')} {dt.strftime('%H:%M')} — {name}"
        lines.append(label)
        keyboard.append([InlineKeyboardButton(label[:40], callback_data=f"{CB_CLIENT}{uid}")])
    keyboard.append(_back_row(CB_CALENDAR))

    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def _render_schedule_grid(update: Update, offset: int) -> None:
    """Show a compact 2-week calendar grid with day-number buttons."""
    q = update.callback_query
    today = datetime.now(tz=KYIV_OFFSET).date()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=offset * 14)

    # Gather which days have bookings (for visual markers).
    busy_days: set = set()
    for b in storage.list_all_bookings():
        if b.get("status") not in ACTIONABLE_STATUSES:
            continue
        slot = storage.get_slot(b["slot_id"]) or {}
        dt = storage.parse_slot(slot.get("datetime", ""))
        if dt:
            busy_days.add(dt.date())

    # Build 2 rows × 7 buttons (14 days total).
    keyboard = []
    for week in range(2):
        row = []
        for wd in range(7):
            day = monday + timedelta(days=week * 7 + wd)
            label = str(day.day)
            if day == today:
                label = f"[{label}]"
            elif day in busy_days:
                label = f"•{label}"
            row.append(InlineKeyboardButton(label, callback_data=f"{CB_DAY}{day.strftime('%Y-%m-%d')}"))
        keyboard.append(row)

    # Navigation: ⬅️ / ➡️ for shifting by 2 weeks.
    nav = []
    if offset > -2:
        nav.append(InlineKeyboardButton(get_text("admin_cal_prev"),
                                        callback_data=f"{CB_SCHEDULE_OFF}{offset - 1}"))
    nav.append(InlineKeyboardButton(get_text("admin_cal_next"),
                                    callback_data=f"{CB_SCHEDULE_OFF}{offset + 1}"))
    keyboard.append(nav)
    keyboard.append(_back_row(CB_CALENDAR))

    range_start = monday.strftime("%d.%m")
    range_end = (monday + timedelta(days=13)).strftime("%d.%m.%Y")
    title = f"{get_text('admin_schedule_title')}\n{range_start} — {range_end}"

    await q.edit_message_text(title, reply_markup=InlineKeyboardMarkup(keyboard))


# ---------------------------------------------------------------------------
# Text-input bridge for the shared text router in handlers.booking
# ---------------------------------------------------------------------------

async def handle_text_if_admin_awaiting(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Consume admin text input for message-composer, reply, and range-add.

    Returns ``True`` if this module handled the update.
    """
    if not _is_admin(update):
        return False
    awaiting = context.user_data.get(AWAIT_KEY)
    if not awaiting or not str(awaiting).startswith("admin_"):
        return False

    text = (update.message.text or "").strip()

    if awaiting == AWAIT_MSG_TEXT:
        await _finish_admin_msg(update, context, text)
        return True
    if awaiting == AWAIT_REPLY_TEXT:
        await _finish_admin_reply(update, context, text)
        return True
    if awaiting == AWAIT_RANGE_DATE:
        await _range_on_date(update, context, text)
        return True
    if awaiting == AWAIT_RANGE_START:
        await _range_on_start(update, context, text)
        return True
    if awaiting == AWAIT_RANGE_END:
        await _range_on_end(update, context, text)
        return True
    if awaiting == AWAIT_RANGE_STEP:
        await _range_on_step(update, context, text)
        return True
    return False


async def _finish_admin_msg(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    user_id = context.user_data.pop("admin_msg_user_id", None)
    context.user_data[AWAIT_KEY] = None
    if not user_id:
        return
    try:
        await context.bot.send_message(
            chat_id=user_id, text=get_text("admin_msg_to_client", message=text)
        )
        await update.message.reply_text(
            get_text("admin_msg_sent"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )
        logger.info("admin dm → user=%s len=%d", user_id, len(text))
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(get_text("admin_msg_failed", error=str(exc)))


async def _finish_admin_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Reply to a client's incoming message (triggered via "Відповісти" button)."""
    user_id = context.user_data.pop("admin_reply_user_id", None)
    context.user_data[AWAIT_KEY] = None
    if not user_id:
        return
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=get_text("admin_reply_to_client", text=text),
        )
        await update.message.reply_text(
            get_text("admin_reply_sent"),
            reply_markup=InlineKeyboardMarkup([_back_row()]),
        )
        logger.info("admin reply → user=%s", user_id)
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(get_text("admin_msg_failed", error=str(exc)))


def _valid_time(s: str) -> datetime | None:
    try:
        return datetime.strptime(s.strip(), "%H:%M")
    except ValueError:
        return None


async def _range_on_date(update, context, text: str) -> None:
    try:
        dt = datetime.strptime(text, "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text(get_text("admin_range_date_error"))
        return
    context.user_data["admin_range_date"] = dt.strftime("%Y-%m-%d")
    context.user_data[AWAIT_KEY] = AWAIT_RANGE_START
    await update.message.reply_text(get_text("admin_range_ask_start"))


async def _range_on_start(update, context, text: str) -> None:
    t = _valid_time(text)
    if t is None:
        await update.message.reply_text(get_text("admin_range_time_error"))
        return
    context.user_data["admin_range_start"] = t.strftime("%H:%M")
    context.user_data[AWAIT_KEY] = AWAIT_RANGE_END
    await update.message.reply_text(get_text("admin_range_ask_end"))


async def _range_on_end(update, context, text: str) -> None:
    t = _valid_time(text)
    if t is None:
        await update.message.reply_text(get_text("admin_range_time_error"))
        return
    context.user_data["admin_range_end"] = t.strftime("%H:%M")
    context.user_data[AWAIT_KEY] = AWAIT_RANGE_STEP
    await update.message.reply_text(get_text("admin_range_ask_step"))


async def _range_on_step(update, context, text: str) -> None:
    if not text.isdigit() or not (1 <= int(text) <= 720):
        await update.message.reply_text(get_text("admin_range_step_error"))
        return
    step = int(text)
    date_iso = context.user_data.pop("admin_range_date")
    start_s = context.user_data.pop("admin_range_start")
    end_s = context.user_data.pop("admin_range_end")
    context.user_data[AWAIT_KEY] = None

    day = datetime.strptime(date_iso, "%Y-%m-%d")
    start_dt = datetime.strptime(f"{date_iso} {start_s}", "%Y-%m-%d %H:%M")
    end_dt = datetime.strptime(f"{date_iso} {end_s}", "%Y-%m-%d %H:%M")
    if end_dt <= start_dt:
        await update.message.reply_text(get_text("admin_range_order_error"))
        return

    count = 0
    cursor = start_dt
    while cursor <= end_dt:
        storage.add_slot(storage.to_storage_format(cursor))
        count += 1
        cursor += timedelta(minutes=step)

    logger.info("admin added %s slots on %s", count, date_iso)
    await update.message.reply_text(
        get_text("admin_range_done", count=count, date=day.strftime("%d.%m.%Y")),
        reply_markup=InlineKeyboardMarkup([_back_row()]),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(app) -> None:
    # Inline admin UI
    app.add_handler(CommandHandler("admin", open_admin_menu))
    app.add_handler(CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_"))

    # Legacy CLI commands (still available as shortcuts)
    app.add_handler(CommandHandler("slots_add", slots_add))
    app.add_handler(CommandHandler("slots_list", slots_list))
    app.add_handler(CommandHandler("slots_del", slots_del))
    app.add_handler(CommandHandler("send", send_to_user))
    app.add_handler(CommandHandler("bookings", bookings))