"""ACOCOS Telegram bot (aiogram 3, long polling).

Simple by design: /stock and /today, allowlisted users only. It shares the exact
same Django models and reply layer as the WhatsApp webhook — no duplicated logic.

Run: python bot/main.py  (or the `bot` service in docker compose)
Add users: create BotUser rows in the panel with their Telegram ID.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.filters import Command, CommandObject, CommandStart  # noqa: E402
from aiogram.types import Message  # noqa: E402
from asgiref.sync import sync_to_async  # noqa: E402
from django.conf import settings  # noqa: E402

from apps.core.models import BotUser  # noqa: E402
from apps.wa.replies import HELP, stock_reply, today_reply  # noqa: E402

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()


@sync_to_async
def get_allowed_user(telegram_id: int) -> BotUser | None:
    return BotUser.objects.filter(telegram_id=telegram_id, is_active=True).first()


async def guard(message: Message) -> BotUser | None:
    """Allowlist check. Unknown users get silence — the bot doesn't reveal it exists."""
    user = await get_allowed_user(message.from_user.id)
    if user is None:
        logging.warning("Ignored message from unknown Telegram ID %s", message.from_user.id)
    return user


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if await guard(message) is None:
        return
    await message.answer(f"ACOCOS CRM bot.\n{HELP}")


@dp.message(Command("stock"))
async def cmd_stock(message: Message, command: CommandObject):
    if await guard(message) is None:
        return
    query = (command.args or "").strip()
    text = await sync_to_async(stock_reply)(query)
    await message.answer(text)


@dp.message(Command("today"))
async def cmd_today(message: Message):
    if await guard(message) is None:
        return
    text = await sync_to_async(today_reply)()
    await message.answer(text)


@dp.message()
async def fallback(message: Message):
    if await guard(message) is None:
        return
    await message.answer(HELP)


async def main():
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env — bot not started.")
    bot = Bot(token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
