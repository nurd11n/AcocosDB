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
    """The WhatsApp receipt. Shows what was bought, what was paid, and what is
    still owed — a receipt that stops at «Итого» leaves the client with no
    record of a balance they're carrying, which is exactly the number they
    ask about later. `balance` is derived (total − payments, converted at each
    payment's frozen rate), so it always agrees with the POS and the debts
    report rather than being a second, drifting copy."""
    lines = [_("Спасибо за покупку в ACOCOS! 🌸"), ""]
    for item in order.items.select_related("variant__product"):
        line = f"• {item.variant} — {item.quantity} × {item.unit_price} {order.currency}"
        # Only spelled out when a discount actually applies — for the common
        # undiscounted line, qty × unit_price already equals what "Итого"
        # sums, so adding "= line_total" here would just repeat it.
        if item.discount_amount > 0:
            line += (
                f" (−{item.discount_amount} {order.currency}) = {item.line_total} {order.currency}"
            )
        lines.append(line)
    lines.append("")
    lines.append(_("Итого: %(total)s %(cur)s") % {"total": order.total, "cur": order.currency})

    paid = order.paid_amount
    balance = order.balance
    if paid > 0:
        lines.append(_("Оплачено: %(paid)s %(cur)s") % {"paid": paid, "cur": order.currency})
    if balance > 0:
        lines.append(
            _("Остаток к оплате: %(bal)s %(cur)s") % {"bal": balance, "cur": order.currency}
        )
        # The client's TOTAL across every unpaid sale — only worth saying when
        # it differs from this one sale's balance, otherwise it just repeats
        # the line above.
        if order.client_id:
            from apps.clients.services import client_debt

            total_debt = client_debt(order.client).get(order.currency)
            if total_debt and total_debt != balance:
                lines.append(
                    _("Общий долг: %(debt)s %(cur)s") % {"debt": total_debt, "cur": order.currency}
                )
    elif paid > 0:
        lines.append(_("Оплачено полностью. Спасибо!"))
    return "\n".join(lines)


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
