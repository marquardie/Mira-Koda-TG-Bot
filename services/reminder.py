"""Scheduled jobs: 30-min reminder + post-session bookkeeping.

Two jobs are scheduled per confirmed booking:

* ``reminder:<booking_id>`` — fires at ``slot - 30min``. Sends the user the
  nagадування with a meeting link.
* ``session_end:<booking_id>`` — fires at ``slot + 1.5h``. Marks the session
  as done, increments ``sessions_completed``, and — when the user reaches 3
  sessions — sends the package offer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram.ext import Application, ContextTypes

from config import MEETING_LINK, REMINDER_MINUTES_BEFORE
from services import storage
from services.texts import get_text

logger = logging.getLogger(__name__)

# Kyiv (UTC+3 without DST for this MVP).
KYIV_OFFSET = timezone(timedelta(hours=3))

# Hours after slot start when we consider the session "finished" and fire the
# post-session job. Matches the spec: "after session time + 1.5 hours".
SESSION_END_HOURS_AFTER = 1.5

# How many completed sessions unlock the package offer.
PACKAGE_THRESHOLD_SESSIONS = 3


def _parse_slot_tz(dt_str: str) -> datetime | None:
    naive = storage.parse_slot(dt_str)
    return naive.replace(tzinfo=KYIV_OFFSET) if naive else None


# ---------------------------------------------------------------------------
# Job: reminder (slot - 30 min)
# ---------------------------------------------------------------------------

async def _send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.common import format_slot_human

    data = context.job.data or {}
    user_id = data["user_id"]
    slot_str = data["slot"]
    await context.bot.send_message(
        chat_id=user_id,
        text=get_text("reminder", slot=format_slot_human(slot_str), link=MEETING_LINK),
    )
    logger.info("reminder sent user=%s slot=%s", user_id, slot_str)


def schedule_reminder(
    app: Application, user_id: int, booking_id: int, slot_str: str
) -> bool:
    """Schedule the pre-session reminder via ``job_queue.run_once(delay_seconds)``.

    * ``reminder_time = session_time - REMINDER_MINUTES_BEFORE``
    * If ``reminder_time`` is **already in the past** (e.g. bot restarted late
      or admin only just confirmed payment) — fire immediately so the user
      never misses the meeting link.
    * Idempotent by ``booking_id``.
    """
    slot_dt = _parse_slot_tz(slot_str)
    if slot_dt is None:
        logger.info("reminder not scheduled booking=%s — unparsable slot %r", booking_id, slot_str)
        return False

    job_name = f"reminder:{booking_id}"
    if app.job_queue.get_jobs_by_name(job_name):
        logger.info("reminder dedup booking=%s — already queued", booking_id)
        return False

    now = datetime.now(tz=KYIV_OFFSET)
    reminder_time = slot_dt - timedelta(minutes=REMINDER_MINUTES_BEFORE)

    if reminder_time <= now:
        # Past the 30-min mark — fire ASAP so the user still gets the link.
        delay_seconds = 1.0
        logger.info("reminder firing immediately booking=%s — reminder_time already passed", booking_id)
    else:
        delay_seconds = (reminder_time - now).total_seconds()

    app.job_queue.run_once(
        _send_reminder,
        delay_seconds,
        data={"user_id": user_id, "slot": slot_str, "booking_id": booking_id},
        name=job_name,
    )
    logger.info(
        "reminder scheduled booking=%s user=%s fires in %.0fs at %s",
        booking_id, user_id, delay_seconds, reminder_time,
    )
    return True


# ---------------------------------------------------------------------------
# Job: session_end (slot + 1.5h) — marks done + triggers package offer
# ---------------------------------------------------------------------------

async def _on_session_end(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark the booking done, bump counters, and maybe send the package offer."""
    data = context.job.data or {}
    user_id = data["user_id"]
    booking_id = data.get("booking_id")
    if not booking_id:
        return

    booking = storage.get_booking(booking_id)
    # Only promote confirmed → done. Cancelled bookings stay cancelled.
    if not booking or booking["status"] != "confirmed":
        logger.info("session_end skipped booking=%s status=%s", booking_id, booking and booking.get("status"))
        return

    storage.update_booking(booking_id, status="done")
    new_count = storage.increment_sessions_completed(user_id)
    logger.info("session completed booking=%s user=%s total=%s", booking_id, user_id, new_count)

    # One-shot package offer when the user hits the 3-session milestone.
    user = storage.get_user_with_defaults(user_id) or {}
    if new_count >= PACKAGE_THRESHOLD_SESSIONS and not user.get("package_offered") \
            and not user.get("package_active"):
        # Local import — payment module imports back from us indirectly.
        from handlers.payment import package_offer_keyboard
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=get_text("package_offer_card"),
                reply_markup=package_offer_keyboard(),
            )
            storage.mark_package_offered(user_id)
            logger.info("package offer sent user=%s", user_id)
        except Exception:  # noqa: BLE001
            logger.exception("failed to send package offer to user=%s", user_id)


def schedule_session_end(
    app: Application, user_id: int, booking_id: int, slot_str: str
) -> bool:
    """Schedule the post-session job at ``slot + 1.5h`` via ``delay_seconds``."""
    slot_dt = _parse_slot_tz(slot_str)
    if slot_dt is None:
        return False

    now = datetime.now(tz=KYIV_OFFSET)
    run_at = slot_dt + timedelta(hours=SESSION_END_HOURS_AFTER)
    if run_at <= now:
        return False

    job_name = f"session_end:{booking_id}"
    if app.job_queue.get_jobs_by_name(job_name):
        return False

    delay_seconds = (run_at - now).total_seconds()
    app.job_queue.run_once(
        _on_session_end,
        delay_seconds,
        data={"user_id": user_id, "slot": slot_str, "booking_id": booking_id},
        name=job_name,
    )
    logger.info("session_end scheduled booking=%s user=%s fires in %.0fs", booking_id, user_id, delay_seconds)
    return True


# ---------------------------------------------------------------------------
# Restore after restart (PTB's JobQueue is in-memory)
# ---------------------------------------------------------------------------

def restore_reminders(app: Application) -> int:
    """Re-queue both reminder and session_end jobs for all confirmed bookings."""
    restored = 0
    for booking in storage.list_all_bookings():
        if booking.get("status") != "confirmed":
            continue
        slot = storage.get_slot(booking["slot_id"])
        if not slot:
            continue
        if schedule_reminder(app, booking["user_id"], booking["id"], slot["datetime"]):
            restored += 1
        schedule_session_end(app, booking["user_id"], booking["id"], slot["datetime"])
    logger.info("restored %s reminders after restart", restored)
    return restored


# ---------------------------------------------------------------------------
# Slot-hold release: cancel pending bookings if the user never pays
# ---------------------------------------------------------------------------

async def _release_unpaid_slot(context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.common import format_slot_human

    data = context.job.data or {}
    booking_id = data.get("booking_id")
    if not booking_id:
        return

    booking = storage.get_booking(booking_id)
    if not booking:
        return
    # Only release if still in pending_payment. If the user already pressed
    # "Я оплатив" the status is waiting_confirm — admin owns it then.
    if booking.get("status") != "pending_payment":
        return

    storage.update_booking(booking_id, status="cancelled")
    storage.mark_slot_booked(booking["slot_id"], booked=False)
    slot = storage.get_slot(booking["slot_id"]) or {}
    slot_human = format_slot_human(slot.get("datetime", "—")) if slot else "—"
    logger.info("slot auto-released booking=%s user=%s", booking_id, booking["user_id"])

    try:
        await context.bot.send_message(
            chat_id=booking["user_id"],
            text=get_text("slot_released_timeout", slot=slot_human),
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to notify user about slot auto-release")


def schedule_slot_release(
    app: Application, booking_id: int, hold_minutes: int
) -> bool:
    """Schedule auto-release of a held slot ``hold_minutes`` from now."""
    job_name = f"slot_release:{booking_id}"
    if app.job_queue.get_jobs_by_name(job_name):
        return False
    app.job_queue.run_once(
        _release_unpaid_slot,
        hold_minutes * 60,
        data={"booking_id": booking_id},
        name=job_name,
    )
    logger.info("slot-release scheduled booking=%s in %dmin", booking_id, hold_minutes)
    return True


def cancel_slot_release(app: Application, booking_id: int) -> None:
    """Drop the pending slot-release job (e.g. once user paid)."""
    for job in app.job_queue.get_jobs_by_name(f"slot_release:{booking_id}"):
        job.schedule_removal()
