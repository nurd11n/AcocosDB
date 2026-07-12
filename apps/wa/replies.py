"""Shared bilingual command handling — used by both the WhatsApp and Telegram bots."""

from django.db.models import Q

from apps.inventory.models import ProductVariant
from apps.sales.services import today_summary

HELP = (
    "Commands / Команды:\n"
    "stock <sku or name> — current stock / остаток\n"
    "today — today's sales / продажи за сегодня"
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


def build_reply(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith(("stock", "остаток")):
        return stock_reply(text.split(maxsplit=1)[1].strip() if " " in text else "")
    if lowered.startswith(("today", "сегодня")):
        return today_reply()
    return HELP
