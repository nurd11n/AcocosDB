"""Pre-filled WhatsApp message helpers for the POS — a receipt after a sale and
a polite debt reminder. These build a wa.me deep link the manager taps to open
WhatsApp with the text ready to send; nothing is sent automatically (that's the
campaigns feature, with consent). Each tap is logged as a client Interaction.
"""

from decimal import Decimal
from urllib.parse import quote

from django.utils.translation import gettext as _


def wa_link(phone: str, text: str) -> str:
    """wa.me needs digits only, international, no '+'. Empty phone -> ''."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return ""
    return f"https://wa.me/{digits}?text={quote(text)}"


def receipt_text(order) -> str:
    lines = [_("Спасибо за покупку в ACOCOS! 🌸"), ""]
    for item in order.items.select_related("variant__product"):
        lines.append(f"• {item.variant} — {item.quantity} × {item.unit_price} {order.currency}")
    lines.append("")
    lines.append(_("Итого: %(total)s %(cur)s") % {"total": order.total, "cur": order.currency})
    return "\n".join(lines)


def debt_reminder_text(client, debts: dict[str, Decimal]) -> str:
    amounts = ", ".join(f"{amt} {cur}" for cur, amt in debts.items())
    return _(
        "Здравствуйте, %(name)s! Напоминаем о задолженности %(amounts)s. Спасибо! — ACOCOS"
    ) % {"name": client.first_name or client.name, "amounts": amounts}
