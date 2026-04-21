"""Entry point for the Mira Koda booking bot.

Run:
    python main.py

Required env (see .env.example):
    BOT_TOKEN, ADMIN_ID, MEETING_LINK
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler

from config import BOT_TOKEN, ensure_config
from handlers import admin, booking, payment
from handlers.start import build_start_conversation, help_cmd, profile
from services.reminder import restore_reminders

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mira-bot")


async def _post_init(app: Application) -> None:
    """Runs once after the bot is built — re-schedules persistent reminders."""
    restore_reminders(app)
    logger.info("post_init done, bot is ready")


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    # /start + questionnaire conversation (must be registered before the
    # generic message router in handlers.booking).
    app.add_handler(build_start_conversation())

    # Plain commands
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("help", help_cmd))

    # Feature modules
    admin.register(app)     # admin commands
    payment.register(app)   # callback queries for payment + admin confirm/reject
    booking.register(app)   # book/my/slot callbacks + reply-keyboard router (last)

    return app


def main() -> None:
    ensure_config()
    app = build_app()
    logger.info("Bot is starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()