"""Shared bilingual command handling — used by both the WhatsApp and Telegram bots."""

from django.db.models import Q

from apps.clients.models import Client
from apps.clients.services import client_debt, clients_with_debt
from apps.inventory.models import ProductVariant
from apps.sales.models import SaleOrder
from apps.sales.services import today_summary

HELP = (
    "Commands / Команды:\n"
    "stock <sku or name> — current stock / остаток\n"
    "today — today's sales / продажи за сегодня\n"
    "client <phone> — purchases + debt / клиент\n"
    "debts — top debtors / долги"
)


def stock_reply(query: str) -> str:
    if not query:
        return HELP
    variants = ProductVariant.objects.filter(
        Q(sku__icontains=query) | Q(product__name__icontains=query), is_active=True
    ).select_related("product")[:10]
    if not variants:
        return f"Nothing found for '{query}' / Ничего не найдено"
    return "\n".join(f"{v} [{v.sku}]: {v.stock}" for v in variants)


def today_reply() -> str:
    s = today_summary()
    return (
        f"Today / Сегодня: {s['orders']} sales / продаж, "
        f"{s['items']} items / шт, revenue / выручка {s['revenue']}"
    )


def client_reply(phone: str) -> str:
    if not phone:
        return HELP
    client = Client.objects.filter(phone__icontains=phone).first()
    if client is None:
        return f"No client found for '{phone}' / Клиент не найден"
    orders = client.sales.filter(status=SaleOrder.CONFIRMED).order_by("-confirmed_at")[:5]
    lines = [f"{client.name} ({client.phone}) — debt/долг: {client_debt(client)}"]
    for order in orders:
        lines.append(f"#{order.pk} {order.confirmed_at:%Y-%m-%d} — {order.total}")
    if not orders:
        lines.append("No confirmed purchases yet / Пока нет покупок")
    return "\n".join(lines)


def debts_reply() -> str:
    rows = sorted(
        (
            (c.sales_total - c.payments_total, c)
            for c in clients_with_debt()
            if c.sales_total - c.payments_total > 0
        ),
        key=lambda row: row[0],
        reverse=True,
    )[:10]
    if not rows:
        return "No outstanding debts / Нет задолженностей"
    return "\n".join(f"{c.name} ({c.phone}): {debt}" for debt, c in rows)


def build_reply(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith(("stock", "остаток")):
        return stock_reply(text.split(maxsplit=1)[1].strip() if " " in text else "")
    if lowered.startswith(("today", "сегодня")):
        return today_reply()
    if lowered.startswith(("client", "клиент")):
        return client_reply(text.split(maxsplit=1)[1].strip() if " " in text else "")
    if lowered.startswith(("debts", "долги")):
        return debts_reply()
    return HELP
