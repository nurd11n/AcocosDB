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


def receipt_share_text(order) -> str:
    """Short, link-free WhatsApp message — the receipt itself is a PDF she
    attaches to this same chat manually (see apps.pos.receipts /
    views.receipt_download); wa.me can't attach a file, and the text must
    never carry a URL, domain, or token pointing back at this server (see
    CLAUDE.md's receipt rework). Client-facing, so first_name only (see
    Client.name's docstring — the staff-only descriptor must never reach the
    client)."""
    name = order.client.first_name if order.client_id else ""
    if name:
        return _("Спасибо за покупку, %(name)s! Чек прикреплён.") % {"name": name}
    return _("Спасибо за покупку в ACOCOS! Чек прикреплён.")


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
