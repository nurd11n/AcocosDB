"""Staff-only bilingual command handling — used EXCLUSIVELY by bot/staff_bot.py
(the internal, allowlisted Telegram bot). Never wired into the public WhatsApp
webhook or the client Telegram bot: every function here accepts an arbitrary
query/phone and returns aggregate business data (revenue, debts, stock
totals). The WhatsApp client channel uses apps.wa.client_replies instead,
which is scoped to a single resolved Client and holds no aggregate lookups.
"""

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from apps.clients.models import Client
from apps.clients.services import client_debt, debtors_report_rows, lapsed_clients
from apps.inventory.models import ProductVariant
from apps.inventory.services import low_stock_variants
from apps.sales.models import SaleOrder
from apps.sales.services import today_summary

HELP = (
    "Commands / Команды:\n"
    "stock <sku or name> — current stock / остаток\n"
    "today — today's sales / продажи за сегодня\n"
    "client <phone> — purchases + debt / клиент\n"
    "debts — top debtors / долги\n"
    "restock — low stock / мало на складе\n"
    "lapsed — inactive clients / давно не покупали"
)


def stock_reply(query: str) -> str:
    if not query:
        return HELP
    # _stock annotated in SQL — the .stock property reads it, so the reply
    # costs one query for any number of matches, not one per variant.
    variants = (
        ProductVariant.objects.filter(
            Q(sku__icontains=query) | Q(product__name__icontains=query), is_active=True
        )
        .select_related("product")
        .annotate(_stock=Coalesce(Sum("movements__quantity"), 0))[:10]
    )
    if not variants:
        return f"Nothing found for '{query}' / Ничего не найдено"
    return "\n".join(f"{v} [{v.sku}]: {v.stock}" for v in variants)


def today_reply() -> str:
    s = today_summary()
    revenue = ", ".join(f"{amt} {cur}" for cur, amt in s["revenue_by_currency"].items()) or "0"
    return (
        f"Today / Сегодня: {s['orders']} sales / продаж, "
        f"{s['items']} items / шт, revenue / выручка {revenue}"
    )


def client_reply(phone: str) -> str:
    if not phone:
        return HELP
    client = Client.objects.filter(phone__icontains=phone).first()
    if client is None:
        return f"No client found for '{phone}' / Клиент не найден"
    orders = client.sales.filter(status=SaleOrder.CONFIRMED).order_by("-confirmed_at")[:5]
    debts = client_debt(client)
    debt_str = ", ".join(f"{amt} {cur}" for cur, amt in debts.items()) or "0"
    lines = [f"{client.name} ({client.phone}) — debt/долг: {debt_str}"]
    for order in orders:
        lines.append(f"#{order.pk} {order.confirmed_at:%Y-%m-%d} — {order.total} {order.currency}")
    if not orders:
        lines.append("No confirmed purchases yet / Пока нет покупок")
    return "\n".join(lines)


def debts_reply() -> str:
    rows = debtors_report_rows()[:10]
    if not rows:
        return "No outstanding debts / Нет задолженностей"
    return "\n".join(f"{c.name} ({c.phone}): {debt} {cur}" for c, cur, debt, _lp in rows)


def restock_reply() -> str:
    """Variants at or below their low-stock threshold — the 'order more of
    these' list, so a bestseller running out is noticed before it's gone."""
    variants = low_stock_variants()
    if not variants:
        return "Stock levels OK / Остатки в норме"
    lines = ["Low stock / Мало на складе:"]
    lines.extend(f"{v} [{v.sku}]: {v.stock} / {v.low_stock_threshold}" for v in variants)
    return "\n".join(lines)


def lapsed_reply(days: int = 60) -> str:
    """Clients who bought before but not in the last `days` days — who to
    re-engage. The campaign 'inactive for N days' filter targets this segment."""
    clients = lapsed_clients(days)
    if not clients:
        return f"No clients inactive {days}+ days / Нет клиентов без покупок {days}+ дней"
    lines = [f"Inactive {days}+ days / Давно не покупали ({days}+ дн.):"]
    lines.extend(f"{c.name} ({c.phone})" for c in clients[:20])
    return "\n".join(lines)
