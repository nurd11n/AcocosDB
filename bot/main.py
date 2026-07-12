"""ACOCOS Telegram bot (aiogram 3, long polling).

/stock, /today, /client, /debts — allowlisted users only. It shares the exact
same Django models and reply layer as the WhatsApp webhook — no duplicated logic.
Every incoming/outgoing message is logged to apps.core.models.BotMessage.

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

from apps.core.models import BotMessage, BotUser  # noqa: E402
from apps.wa.replies import HELP, client_reply, debts_reply, stock_reply, today_reply  # noqa: E402

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()


@sync_to_async
def get_allowed_user(telegram_id: int) -> BotUser | None:
    return BotUser.objects.filter(telegram_id=telegram_id, is_active=True).first()


@sync_to_async
def log_message(telegram_id: int, direction: str, text: str) -> None:
    BotMessage.objects.create(
        channel=BotMessage.TELEGRAM, external_id=str(telegram_id), direction=direction, text=text
    )


async def guard(message: Message) -> BotUser | None:
    """Allowlist check. Unknown users get silence — the bot doesn't reveal it exists."""
    user = await get_allowed_user(message.from_user.id)
    if user is None:
        logging.warning("Ignored message from unknown Telegram ID %s", message.from_user.id)
        return None
    await log_message(message.from_user.id, BotMessage.IN, message.text or "")
    return user


async def reply(message: Message, text: str) -> None:
    await log_message(message.from_user.id, BotMessage.OUT, text)
    await message.answer(text)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if await guard(message) is None:
        return
    await reply(message, f"ACOCOS CRM bot.\n{HELP}")


@dp.message(Command("stock"))
async def cmd_stock(message: Message, command: CommandObject):
    if await guard(message) is None:
        return
    query = (command.args or "").strip()
    text = await sync_to_async(stock_reply)(query)
    await reply(message, text)


@dp.message(Command("today"))
async def cmd_today(message: Message):
    if await guard(message) is None:
        return
    text = await sync_to_async(today_reply)()
    await reply(message, text)


@dp.message(Command("client"))
async def cmd_client(message: Message, command: CommandObject):
    if await guard(message) is None:
        return
    query = (command.args or "").strip()
    text = await sync_to_async(client_reply)(query)
    await reply(message, text)


@dp.message(Command("debts"))
async def cmd_debts(message: Message):
    if await guard(message) is None:
        return
    text = await sync_to_async(debts_reply)()
    await reply(message, text)


@dp.message()
async def fallback(message: Message):
    if await guard(message) is None:
        return
    await reply(message, HELP)


async def main():
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env — bot not started.")
    bot = Bot(token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
