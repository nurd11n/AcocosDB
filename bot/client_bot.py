"""ACOCOS client Telegram bot — public, anyone may /start.

This is the acquisition mechanism for Telegram campaigns (see CLAUDE.md
"Рассылки"): Telegram cannot message anyone who hasn't pressed /start, so
subscribing here is the whole audience-building story.

Deliberately isolated from bot/staff_bot.py: this module imports NOTHING from
apps.wa.replies (the staff query layer) and holds no reference to BotUser. A
future handler here (catalog, own orders, own debt — Part C) may look up data
for the CURRENT chat's own Client only; it must never call a staff reply
function or an aggregate service (today_summary, debtors_report_rows, etc.).
tests/test_bots.py enforces the current, data-free state of this module and
must be extended alongside any future handler that touches client data.

Run standalone: python -m bot.client_bot  (normally started by bot/main.py
alongside the staff bot, in the same process).
"""

from aiogram import Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from asgiref.sync import sync_to_async

from apps.clients.services import subscribe_telegram, unsubscribe_telegram

dp = Dispatcher()

_CONTACT_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Добро пожаловать в ACOCOS! 🌸\n"
        "Поделитесь номером телефона, чтобы первыми узнавать о новинках и акциях.\n"
        "Отписаться можно в любой момент — отправьте «СТОП».",
        reply_markup=_CONTACT_KB,
    )


@dp.message(F.contact)
async def on_contact(message: Message):
    phone = message.contact.phone_number
    client = await sync_to_async(subscribe_telegram)(phone, message.chat.id)
    if client is None:
        await message.answer(
            "Спасибо! Мы пока не нашли ваш номер в базе — загляните к нам, и мы вас добавим."
        )
    else:
        await message.answer("Готово! Вы подписаны на новости ACOCOS. 💌")


@dp.message(F.text.lower().in_({"стоп", "stop"}))
async def on_stop(message: Message):
    await sync_to_async(unsubscribe_telegram)(message.chat.id)
    await message.answer("Вы отписались от рассылки. Спасибо, что были с нами!")
