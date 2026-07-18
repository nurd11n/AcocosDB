"""ACOCOS staff Telegram bot — allowlisted BotUsers only.

Exposes stock, revenue, debts, and reports: everything that would damage the
business if it leaked. The allowlist is enforced by an OUTER MIDDLEWARE on the
message observer, not by per-handler checks — every handler in this module,
including any added later, is gated before it can run, and an unknown Telegram
ID gets silence, never an error or a hint that the bot exists. This module
must never import anything from bot/client_bot.py, and must never be reachable
from it — see tests/test_bots.py, which asserts the two dispatchers share no
command surface.

Run standalone: python -m bot.staff_bot  (normally started by bot/main.py
alongside the client bot, in the same process).
"""

import logging

from aiogram import BaseMiddleware, Dispatcher
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from asgiref.sync import sync_to_async

from apps.core.models import BotMessage, BotUser
from apps.wa.replies import (
    HELP,
    client_reply,
    debts_reply,
    lapsed_reply,
    restock_reply,
    stock_reply,
    today_reply,
)

logger = logging.getLogger(__name__)
dp = Dispatcher()


@sync_to_async
def get_allowed_user(telegram_id: int) -> BotUser | None:
    return BotUser.objects.filter(telegram_id=telegram_id, is_active=True).first()


@sync_to_async
def log_message(telegram_id: int, direction: str, text: str) -> None:
    BotMessage.objects.create(
        channel=BotMessage.TELEGRAM, external_id=str(telegram_id), direction=direction, text=text
    )


class AllowlistMiddleware(BaseMiddleware):
    """Silently drop any message from a Telegram ID without an active BotUser
    row, and log allowed traffic. Runs BEFORE filters and handlers, so no
    staff handler — present or future — can be reached by a stranger."""

    async def __call__(self, handler, event: Message, data: dict):
        user = await get_allowed_user(event.from_user.id)
        if user is None:
            logger.warning("Ignored message from unknown Telegram ID %s", event.from_user.id)
            return None  # silence — the bot doesn't reveal it exists
        await log_message(event.from_user.id, BotMessage.IN, event.text or "")
        data["bot_user"] = user  # handlers may use flags like can_see_costs
        return await handler(event, data)


dp.message.outer_middleware(AllowlistMiddleware())


async def reply(message: Message, text: str) -> None:
    await log_message(message.from_user.id, BotMessage.OUT, text)
    await message.answer(text)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await reply(message, f"ACOCOS CRM bot.\n{HELP}")


@dp.message(Command("stock"))
async def cmd_stock(message: Message, command: CommandObject):
    text = await sync_to_async(stock_reply)((command.args or "").strip())
    await reply(message, text)


@dp.message(Command("today"))
async def cmd_today(message: Message):
    await reply(message, await sync_to_async(today_reply)())


@dp.message(Command("client"))
async def cmd_client(message: Message, command: CommandObject):
    text = await sync_to_async(client_reply)((command.args or "").strip())
    await reply(message, text)


@dp.message(Command("debts"))
async def cmd_debts(message: Message):
    await reply(message, await sync_to_async(debts_reply)())


@dp.message(Command("restock"))
async def cmd_restock(message: Message):
    await reply(message, await sync_to_async(restock_reply)())


@dp.message(Command("lapsed"))
async def cmd_lapsed(message: Message):
    await reply(message, await sync_to_async(lapsed_reply)())


@dp.message()
async def fallback(message: Message):
    await reply(message, HELP)
