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


def receipt_share_text(order, receipt_url: str) -> str:
    """Short WhatsApp message pointing at the receipt WEB PAGE (see
    apps.pos.receipts / apps.pos.public_views) — the itemised breakdown,
    discount, paid/balance all live on that page now, grouped by product+size
    with colours nested, never pasted as raw text into the chat. Client-
    facing, so first_name only (see Client.name's docstring — the staff-only
    descriptor must never reach the client)."""
    name = order.client.first_name if order.client_id else ""
    greeting = (
        _("Спасибо за покупку, %(name)s! 🌸") % {"name": name}
        if name
        else _("Спасибо за покупку в ACOCOS! 🌸")
    )
    return f"{greeting}\n{_('Ваш чек')}: {receipt_url}"


def debt_reminder_text(client, debts: dict[str, Decimal]) -> str:
    # first_name ONLY — never client.name, which parenthesises the staff-only
    # descriptor ("Айгуль (сестра Розы)") onto it. The descriptor exists so
    # STAFF can tell clients apart in a list; a client must never see it about
    # themselves. Fall back to phone, not .name, on the practically-never-
    # empty-first_name edge case (see apps.clients.services: a client created
    # from just a phone number gets first_name=phone as its placeholder).
    amounts = ", ".join(f"{amt} {cur}" for cur, amt in debts.items())
    return _(
        "Здравствуйте, %(name)s! Напоминаем о задолженности %(amounts)s. Спасибо! — ACOCOS"
    ) % {"name": client.first_name or client.phone, "amounts": amounts}
