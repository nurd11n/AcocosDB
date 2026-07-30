"""Client-safe WhatsApp reply builder (CLIENT_BOTS.md §4).

Unlike apps.wa.replies (the staff-only query layer used exclusively by
bot/staff_bot.py), this module must never expose an aggregate or another
client's data — everything here is scoped to the ONE Client the inbound
message was matched to. Assume someone typing a sentence, not navigating a
menu: no guided multi-step flow, no LLM intent parsing — a dumb keyword
matcher that stays silent beats one that guesses wrong.
"""

import re

from apps.clients.services import set_marketing_consent, start_handoff
from apps.inbox.services import ORDER_STATUS_RU, bot_content, create_order_request
from apps.inventory.models import Product
from apps.inventory.services import available_for
from apps.orders.models import Order
from apps.orders.services import order_paid_amount

FOOTER = "\n\nНапишите «менеджер» — ответит человек."

MENU_TEXT = "1 — Каталог\n2 — Мой заказ\n3 — Оплата и доставка\n4 — Написать менеджеру"

_GREETINGS = ("привет", "здравствуйте", "добрый день", "добрый вечер", "здравствуй", "меню", "menu")
_ORDER_STATUS_WORDS = ("когда готово", "мой заказ", "мои заказы", "статус заказа")
_INTENT_WORDS = ("хочу заказать", "хочу купить", "закажу", "оформить заказ", "хочу оформить")
_MANAGER_WORDS = ("менеджер", "manager", "человек")
_STOP_WORDS = ("стоп", "stop")


def _with_footer(text: str) -> str:
    return text if text.endswith(FOOTER) else text + FOOTER


def order_status_reply(client) -> str:
    orders = list(
        Order.objects.filter(client=client, status__in=Order.OPEN_STATUSES).order_by("due_date")
    )
    sales = list(client.sales.filter(status="confirmed").order_by("-confirmed_at")[:3])
    if not orders and not sales:
        return _with_footer("У вас пока нет заказов.")
    lines = []
    for o in orders:
        paid = order_paid_amount(o)
        remaining = max(o.total - paid, 0)
        due = f" · срок {o.due_date:%d.%m.%Y}" if o.due_date else ""
        lines.append(
            f"№{o.pk} — {ORDER_STATUS_RU.get(o.status, o.status)}{due} · остаток {remaining} {o.currency}"
        )
    for s in sales:
        lines.append(f"Продажа №{s.pk} от {s.confirmed_at:%d.%m.%Y} — {s.total} {s.currency}")
    return _with_footer("Ваши заказы:\n" + "\n".join(lines))


_ORDER_NUMBER_RE = re.compile(r"\bзаказ\s*№?\s*(\d+)\b")


def _order_status_from_text(client, text: str) -> str | None:
    # \bзаказ\b (word boundary) deliberately does NOT match "заказать"/
    # "заказал" — those are заявка-intent verbs, handled separately, never
    # confused with "заказ №14" style status lookups.
    match = _ORDER_NUMBER_RE.search(text)
    if match:
        number = match.group(1)
        order = Order.objects.filter(client=client, pk=int(number)).first()
        if order is None:
            return _with_footer(f"Заказ №{number} не найден среди ваших заказов.")
        paid = order_paid_amount(order)
        remaining = max(order.total - paid, 0)
        return _with_footer(
            f"Заказ №{order.pk}: {ORDER_STATUS_RU.get(order.status, order.status)} · остаток {remaining} {order.currency}"
        )
    if any(w in text for w in _ORDER_STATUS_WORDS):
        return order_status_reply(client)
    return None


def catalog_reply(query: str) -> str:
    if not query:
        return _with_footer("Напишите название товара, чтобы узнать цену и наличие.")
    products = Product.objects.filter(is_active=True, name__icontains=query).prefetch_related(
        "variants"
    )[:3]
    if not products:
        return _with_footer(f"Ничего не нашли по запросу «{query}».")
    lines = []
    for p in products:
        variants = [v for v in p.variants.all() if v.is_active]
        prices = {f"{v.sale_price} {v.currency}" for v in variants}
        price_str = next(iter(prices)) if len(prices) == 1 else ""
        avail = ", ".join(
            f"{v.size or v.color or v.sku} — {'есть' if available_for(v) > 0 else 'нет'}"
            for v in variants
        )
        lines.append(f"{p.name} {price_str}\n{avail}".strip())
    return _with_footer("\n\n".join(lines))


def build_client_reply(client, text: str) -> str | None:
    """Returns the reply text, or None to send nothing (already-handed-off or
    a bare acknowledgement handled by the caller)."""
    lowered = text.lower().strip()

    if lowered in _STOP_WORDS:
        # CLIENT_BOTS.md §5.4: «СТОП»/«STOP» unsubscribes instantly across
        # both channels — WhatsApp opt-in mirrors Telegram's marketing_consent.
        set_marketing_consent(client, False)
        client.whatsapp_opted_in = False
        client.save(update_fields=["whatsapp_opted_in"])
        return "Вы отписались от рассылки. Спасибо, что были с нами!"

    if any(w in lowered for w in _MANAGER_WORDS):
        start_handoff(client)
        return "Хорошо! Менеджер ответит вам в ближайшее время."

    if lowered == "1":
        return catalog_reply("")
    if lowered == "2":
        return order_status_reply(client)
    if lowered == "3":
        content = bot_content("delivery")
        if content:
            return _with_footer(content.body_ru)
        return _with_footer("Для оплаты и доставки, пожалуйста, напишите «менеджер».")
    if lowered == "4":
        start_handoff(client)
        return "Хорошо! Менеджер ответит вам в ближайшее время."

    status = _order_status_from_text(client, lowered)
    if status is not None:
        return status

    if any(w in lowered for w in _INTENT_WORDS):
        create_order_request(client, items=[], source="whatsapp", raw_message=text)
        start_handoff(client)
        return _with_footer(
            "Спасибо! Мы получили вашу заявку и свяжемся с вами для подтверждения деталей."
        )

    if any(g in lowered for g in _GREETINGS) or not lowered:
        return _with_footer(MENU_TEXT)

    catalog = catalog_reply(text)
    if "Ничего не нашли" not in catalog:
        return catalog

    return _with_footer(MENU_TEXT)
