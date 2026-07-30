"""ACOCOS client Telegram bot — public, anyone may /start.

This is the acquisition mechanism for Telegram campaigns (see CLAUDE.md
"Рассылки"): Telegram cannot message anyone who hasn't pressed /start, so
subscribing here is the whole audience-building story. CLIENT_BOTS.md §3
adds the actual client experience: a persistent menu, catalog browsing with
live availability, заявка (order request — never reserves stock, never
creates production work by itself), self-service order status, favourites,
back-in-stock alerts, and static «О нас» content.

Deliberately isolated from bot/staff_bot.py: this module imports NOTHING from
apps.wa.replies (the staff query layer) and holds no reference to BotUser.
Every handler here looks up data for the CURRENT chat's own Client only — via
apps.clients.services.client_by_chat_id — and never accepts an arbitrary
client identifier from the user (no "client <phone>" style lookup). See
tests/test_bots.py, extended alongside this module per its own historical
guidance.

Run standalone: python -m bot.client_bot  (normally started by bot/main.py
alongside the staff bot, in the same process).
"""

import logging

from aiogram import Dispatcher, F
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from asgiref.sync import sync_to_async

from apps.clients.services import (
    client_by_chat_id,
    client_debt,
    log_telegram_interaction,
    set_marketing_consent,
    subscribe_telegram,
    unsubscribe_telegram,
)

logger = logging.getLogger(__name__)
dp = Dispatcher()

MENU_CATALOG = "🛍 Каталог"
MENU_ORDERS = "📦 Мои заказы"
MENU_FAVOURITES = "❤️ Избранное"
MENU_WRITE = "💬 Написать нам"
MENU_ABOUT = "ℹ️ О нас"
MENU_LABELS = {MENU_CATALOG, MENU_ORDERS, MENU_FAVOURITES, MENU_WRITE, MENU_ABOUT}

MAIN_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=MENU_CATALOG), KeyboardButton(text=MENU_ORDERS)],
        [KeyboardButton(text=MENU_FAVOURITES), KeyboardButton(text=MENU_WRITE)],
        [KeyboardButton(text=MENU_ABOUT)],
    ],
    resize_keyboard=True,
)

_CONTACT_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

PRODUCTS_PER_PAGE = 5


class OrderRequestFlow(StatesGroup):
    quantity = State()
    note = State()


class WriteToUs(StatesGroup):
    message = State()


# --- sync DB glue, wrapped with sync_to_async at each call site (matches the
# existing subscribe_telegram/unsubscribe_telegram call pattern in this file) ---


def _categories():
    from apps.inventory.models import Category

    return list(Category.objects.order_by("name"))


def _products(category_id=None, query=None, offset=0, limit=PRODUCTS_PER_PAGE):
    from django.db.models import Q

    from apps.inventory.models import Product

    qs = Product.objects.filter(is_active=True)
    if category_id:
        qs = qs.filter(category_id=category_id)
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(variants__sku__icontains=query)).distinct()
    qs = qs.order_by("name")
    total = qs.count()
    return list(qs[offset : offset + limit]), total


def _product_card(product_id):
    from apps.inventory.models import Product
    from apps.inventory.services import available_for

    try:
        product = Product.objects.get(pk=product_id, is_active=True)
    except Product.DoesNotExist:
        return None, []
    variants = list(product.variants.filter(is_active=True))
    rows = [(v, available_for(v)) for v in variants]
    return product, rows


def _variant(variant_id):
    from apps.inventory.models import ProductVariant

    return (
        ProductVariant.objects.filter(pk=variant_id, is_active=True)
        .select_related("product")
        .first()
    )


def _create_request(client, variant, quantity, note):
    from apps.inbox.services import create_order_request

    return create_order_request(
        client, items=[{"variant": variant, "quantity": quantity}], source="telegram", note=note
    )


def _toggle_favourite(client, variant):
    from apps.inbox.services import toggle_favourite

    return toggle_favourite(client, variant)


def _register_stock_alert(client, variant):
    from apps.inbox.services import register_stock_alert

    register_stock_alert(client, variant)


def _favourites(client):
    from apps.inbox.models import Favourite
    from apps.inventory.services import available_for

    rows = list(Favourite.objects.filter(client=client).select_related("variant__product"))
    return [(f, available_for(f.variant)) for f in rows]


def _open_orders_and_sales(client):
    from apps.inbox.services import ORDER_STATUS_RU
    from apps.orders.models import Order

    orders = list(
        Order.objects.filter(client=client, status__in=Order.OPEN_STATUSES).order_by("due_date")
    )
    lines = []
    for o in orders:
        remaining = max(o.total - _order_paid(o), 0)
        lines.append(
            f"№{o.pk} · {ORDER_STATUS_RU.get(o.status, o.status)}"
            + (f" · срок {o.due_date:%d.%m.%Y}" if o.due_date else "")
            + f" · остаток {remaining} {o.currency}"
        )
    return lines


def _order_paid(order):
    from apps.orders.services import order_paid_amount

    return order_paid_amount(order)


def _about_text():
    from apps.inbox.models import BotContent

    parts = list(
        BotContent.objects.filter(
            key__in=["about", "size_guide", "delivery", "returns"], is_active=True
        )
    )
    if not parts:
        return "ACOCOS — платья, костюмы и изделия на заказ. Свяжитесь с нами для подробностей."
    return "\n\n".join(f"<b>{p.title_ru}</b>\n{p.body_ru}" for p in parts)


# --- keyboards ---


def _categories_kb(categories):
    rows = [[InlineKeyboardButton(text=c.name, callback_data=f"cat:{c.pk}:0")] for c in categories]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _products_kb(products, category_id, offset, total):
    rows = [[InlineKeyboardButton(text=p.name, callback_data=f"prod:{p.pk}")] for p in products]
    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="« Назад",
                callback_data=f"cat:{category_id}:{max(offset - PRODUCTS_PER_PAGE, 0)}",
            )
        )
    if offset + PRODUCTS_PER_PAGE < total:
        nav.append(
            InlineKeyboardButton(
                text="Далее »", callback_data=f"cat:{category_id}:{offset + PRODUCTS_PER_PAGE}"
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="« Категории", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _product_kb(product_id, rows):
    buttons = []
    for variant, available in rows:
        label = variant.size or variant.color or variant.sku
        if available > 0:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🛒 Хочу заказать · {label}", callback_data=f"want:{variant.pk}"
                    ),
                    InlineKeyboardButton(text="❤️", callback_data=f"fav:{variant.pk}"),
                ]
            )
        else:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🔔 Сообщить · {label}", callback_data=f"notify:{variant.pk}"
                    ),
                    InlineKeyboardButton(text="❤️", callback_data=f"fav:{variant.pk}"),
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _product_caption(product, rows):
    lines = [f"<b>{product.name}</b>"]
    prices = {f"{v.sale_price} {v.currency}" for v, _a in rows}
    if len(prices) == 1:
        lines.append(next(iter(prices)))
    avail = ", ".join(
        f"{v.size or v.color or v.sku} — {'есть' if a > 0 else 'нет'}" for v, a in rows
    )
    if avail:
        lines.append(avail)
    return "\n".join(lines)


# --- onboarding ---


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    payload = (command.args or "").strip()
    await message.answer(
        "Добро пожаловать в ACOCOS! 🌸\n"
        "Поделитесь номером телефона, чтобы первыми узнавать о новинках и акциях, "
        "и мы сможем находить ваши заказы и историю покупок.",
        reply_markup=_CONTACT_KB,
    )
    await message.answer("Главное меню всегда доступно ниже.", reply_markup=MAIN_MENU_KB)
    if payload.startswith("product_"):
        sku = payload[len("product_") :]
        variant = await sync_to_async(lambda: _variant_by_sku(sku))()
        if variant:
            await _send_product(message, variant.product_id)


def _variant_by_sku(sku):
    from apps.inventory.models import ProductVariant

    return ProductVariant.objects.filter(sku=sku, is_active=True).select_related("product").first()


_CONSENT_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Да", callback_data="consent:yes"),
            InlineKeyboardButton(text="Нет", callback_data="consent:no"),
        ]
    ]
)


@dp.message(F.contact)
async def on_contact(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    client = await sync_to_async(subscribe_telegram)(phone, message.chat.id)
    if client is None:
        await message.answer(
            "Спасибо! Мы пока не нашли ваш номер в базе — загляните к нам, и мы вас добавим.",
            reply_markup=MAIN_MENU_KB,
        )
        return
    await message.answer(f"Добро пожаловать, {client.name}!", reply_markup=MAIN_MENU_KB)
    # Sharing a phone only makes the bot able to reach this chat — consent to
    # actually message with news/promos is a separate, explicit question
    # (CLIENT_BOTS.md §3.1: "never assume consent from /start alone").
    await message.answer("Присылать новинки и акции?", reply_markup=_CONSENT_KB)


@dp.callback_query(F.data.in_({"consent:yes", "consent:no"}))
async def cb_consent(callback: CallbackQuery):
    client = await sync_to_async(client_by_chat_id)(callback.from_user.id)
    if client is not None:
        consent = callback.data == "consent:yes"
        await sync_to_async(set_marketing_consent)(client, consent)
    await callback.message.edit_text(
        "Готово! Вы подписаны на новости ACOCOS. 💌"
        if callback.data == "consent:yes"
        else "Хорошо, новостей присылать не будем."
    )
    await callback.answer()


@dp.message(F.text.lower().in_({"стоп", "stop"}))
async def on_stop(message: Message):
    await sync_to_async(unsubscribe_telegram)(message.chat.id)
    await message.answer("Вы отписались от рассылки. Спасибо, что были с нами!")


# --- каталог ---


@dp.message(F.text == MENU_CATALOG)
async def menu_catalog(message: Message, state: FSMContext):
    await state.clear()
    categories = await sync_to_async(_categories)()
    if not categories:
        await message.answer("Каталог пока пуст.")
        return
    await message.answer("Выберите категорию:", reply_markup=_categories_kb(categories))


@dp.callback_query(F.data == "catalog")
async def cb_catalog(callback: CallbackQuery):
    categories = await sync_to_async(_categories)()
    await callback.message.edit_text("Выберите категорию:", reply_markup=_categories_kb(categories))
    await callback.answer()


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(callback: CallbackQuery):
    _, category_id, offset = callback.data.split(":")
    category_id, offset = int(category_id), int(offset)
    products, total = await sync_to_async(_products)(category_id=category_id, offset=offset)
    if not products:
        await callback.answer("В этой категории пока нет товаров.", show_alert=True)
        return
    await callback.message.edit_text(
        "Товары:", reply_markup=_products_kb(products, category_id, offset, total)
    )
    await callback.answer()


async def _send_product(message: Message, product_id: int):
    product, rows = await sync_to_async(_product_card)(product_id)
    if product is None:
        await message.answer("Товар не найден или больше не активен.")
        return
    caption = _product_caption(product, rows)
    kb = _product_kb(product_id, rows)
    image = product.grid_image
    if image:
        try:
            from aiogram.types import FSInputFile

            await message.answer_photo(
                FSInputFile(image.path), caption=caption, reply_markup=kb, parse_mode="HTML"
            )
            return
        except (FileNotFoundError, ValueError):
            pass
    await message.answer(caption, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("prod:"))
async def cb_product(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    await _send_product(callback.message, product_id)
    await callback.answer()


# --- заявка ---


@dp.callback_query(F.data.startswith("want:"))
async def cb_want(callback: CallbackQuery, state: FSMContext):
    variant_id = int(callback.data.split(":")[1])
    variant = await sync_to_async(_variant)(variant_id)
    if variant is None:
        await callback.answer("Товар недоступен.", show_alert=True)
        return
    client = await sync_to_async(client_by_chat_id)(callback.from_user.id)
    if client is None:
        await callback.message.answer(
            "Чтобы оформить заявку, сначала поделитесь номером телефона.", reply_markup=_CONTACT_KB
        )
        await callback.answer()
        return
    await state.set_state(OrderRequestFlow.quantity)
    await state.update_data(variant_id=variant_id)
    await callback.message.answer(f"Сколько штук «{variant}»?")
    await callback.answer()


@dp.message(StateFilter(OrderRequestFlow.quantity))
async def order_flow_quantity(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Введите количество числом, например: 1")
        return
    await state.update_data(quantity=int(text))
    await state.set_state(OrderRequestFlow.note)
    await message.answer(
        "Комментарий к заявке (например, нужно к дате)? Отправьте «-», чтобы пропустить."
    )


@dp.message(StateFilter(OrderRequestFlow.note))
async def order_flow_note(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    note = "" if (message.text or "").strip() == "-" else (message.text or "").strip()
    variant = await sync_to_async(_variant)(data["variant_id"])
    client = await sync_to_async(client_by_chat_id)(message.from_user.id)
    if client is None or variant is None:
        await message.answer(
            "Не удалось оформить заявку — попробуйте снова из каталога.", reply_markup=MAIN_MENU_KB
        )
        return
    try:
        request = await sync_to_async(_create_request)(client, variant, data["quantity"], note)
    except Exception as exc:  # ValidationError from create_order_request (rate limit)
        await message.answer(str(exc), reply_markup=MAIN_MENU_KB)
        return
    await message.answer(
        f"Заявка №{request.pk} принята. Мы свяжемся с вами для подтверждения.",
        reply_markup=MAIN_MENU_KB,
    )


@dp.callback_query(F.data.startswith("notify:"))
async def cb_notify(callback: CallbackQuery):
    variant_id = int(callback.data.split(":")[1])
    variant = await sync_to_async(_variant)(variant_id)
    client = await sync_to_async(client_by_chat_id)(callback.from_user.id)
    if client is None:
        await callback.message.answer(
            "Сначала поделитесь номером телефона.", reply_markup=_CONTACT_KB
        )
        await callback.answer()
        return
    if variant is not None:
        await sync_to_async(_register_stock_alert)(client, variant)
    await callback.answer("Сообщим, как только появится в наличии!", show_alert=True)


@dp.callback_query(F.data.startswith("fav:"))
async def cb_fav(callback: CallbackQuery):
    variant_id = int(callback.data.split(":")[1])
    variant = await sync_to_async(_variant)(variant_id)
    client = await sync_to_async(client_by_chat_id)(callback.from_user.id)
    if client is None:
        await callback.message.answer(
            "Сначала поделитесь номером телефона.", reply_markup=_CONTACT_KB
        )
        await callback.answer()
        return
    if variant is None:
        await callback.answer("Товар недоступен.", show_alert=True)
        return
    added = await sync_to_async(_toggle_favourite)(client, variant)
    await callback.answer(
        "Добавлено в избранное ❤️" if added else "Убрано из избранного", show_alert=False
    )


# --- мои заказы ---


@dp.message(F.text == MENU_ORDERS)
async def menu_orders(message: Message, state: FSMContext):
    await state.clear()
    client = await sync_to_async(client_by_chat_id)(message.from_user.id)
    if client is None:
        await message.answer("Сначала поделитесь номером телефона.", reply_markup=_CONTACT_KB)
        return
    lines = await sync_to_async(_open_orders_and_sales)(client)
    debt = await sync_to_async(client_debt)(client)
    text_parts = []
    if lines:
        text_parts.append("Ваши заказы:\n" + "\n".join(lines))
    else:
        text_parts.append("Открытых заказов нет.")
    if debt:
        debt_str = ", ".join(f"{amt} {cur}" for cur, amt in debt.items())
        text_parts.append(
            f"Ваш долг: {debt_str}\nДля оплаты свяжитесь с нами — напишите «менеджер»."
        )
    await message.answer("\n\n".join(text_parts), reply_markup=MAIN_MENU_KB)


# --- избранное ---


@dp.message(F.text == MENU_FAVOURITES)
async def menu_favourites(message: Message, state: FSMContext):
    await state.clear()
    client = await sync_to_async(client_by_chat_id)(message.from_user.id)
    if client is None:
        await message.answer("Сначала поделитесь номером телефона.", reply_markup=_CONTACT_KB)
        return
    rows = await sync_to_async(_favourites)(client)
    if not rows:
        await message.answer(
            "Список избранного пуст. Добавляйте товары из каталога ❤️", reply_markup=MAIN_MENU_KB
        )
        return
    lines = [f"{f.variant} — {'есть' if a > 0 else 'нет в наличии'}" for f, a in rows]
    await message.answer("Избранное:\n" + "\n".join(lines), reply_markup=MAIN_MENU_KB)


# --- написать нам ---


@dp.message(F.text == MENU_WRITE)
async def menu_write(message: Message, state: FSMContext):
    await state.set_state(WriteToUs.message)
    await message.answer("Напишите ваше сообщение, и мы свяжемся с вами.")


@dp.message(StateFilter(WriteToUs.message))
async def write_to_us_message(message: Message, state: FSMContext):
    await state.clear()
    client = await sync_to_async(client_by_chat_id)(message.from_user.id)
    if client is not None:
        await sync_to_async(log_telegram_interaction)(client, message.text or "")
        await sync_to_async(_notify_staff_freeform)(client, message.text or "")
    await message.answer("Спасибо! Мы ответим вам в ближайшее время.", reply_markup=MAIN_MENU_KB)


def _notify_staff_freeform(client, text):
    import requests
    from django.conf import settings

    from apps.core.models import BotUser

    if not settings.TELEGRAM_STAFF_TOKEN:
        return
    body = f"💬 {client.name} ({client.phone}): {text}"
    for user in BotUser.objects.filter(is_active=True):
        try:
            requests.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_STAFF_TOKEN}/sendMessage",
                data={"chat_id": user.telegram_id, "text": body},
                timeout=15,
            )
        except requests.RequestException:
            logger.exception("Failed to relay client message to staff")


# --- о нас ---


@dp.message(F.text == MENU_ABOUT)
async def menu_about(message: Message, state: FSMContext):
    await state.clear()
    text = await sync_to_async(_about_text)()
    await message.answer(text, reply_markup=MAIN_MENU_KB, parse_mode="HTML")


# --- text search (fallback, must stay LAST) ---


@dp.message(F.text)
async def text_search(message: Message, state: FSMContext):
    if message.text in MENU_LABELS:
        return  # handled by a dedicated handler above; guards against ordering surprises
    query = message.text.strip()
    products, _total = await sync_to_async(_products)(query=query, limit=5)
    if not products:
        await message.answer(
            "Ничего не найдено. Попробуйте /start или меню ниже.", reply_markup=MAIN_MENU_KB
        )
        return
    if len(products) == 1:
        await _send_product(message, products[0].pk)
        return
    rows = [[InlineKeyboardButton(text=p.name, callback_data=f"prod:{p.pk}")] for p in products]
    await message.answer(
        "Нашли несколько товаров:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
