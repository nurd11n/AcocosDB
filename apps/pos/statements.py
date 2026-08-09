"""Client statement formatting — PDF-only, generated fresh from the database
on every request, same rules as apps.pos.receipts: nothing stored, nothing
written (a plain GET is correct precisely because this is a pure read — see
the row-count test), no signed link, no domain ever sent to a client.

The PDF is a presentation shape over apps.clients.services.client_statement —
that function is the single source of the actual ledger math (reused
verbatim, never re-derived here); this module only adds what's specific to
rendering it as a document (the status badge, first-name-only client text,
the contact line)."""

from django.conf import settings

from apps.clients.models import Client
from apps.clients.services import client_statement
from apps.core.currency import CURRENCY_CODES


def statement_context(client: Client, date_from=None, date_to=None) -> dict:
    """Everything templates/receipts/statement.html needs to render the PDF.

    `sections` is client_statement()'s own {currency: {opening, rows,
    closing}} dict, ordered KGS/USD/RUB (CURRENCY_CODES) — a stable,
    predictable order regardless of dict insertion order, so the PDF and the
    on-screen statement always list currencies the same way.

    `status` drives the header badge — the WORST state across every
    currency (any real debt anywhere outranks a credit, which outranks
    fully settled), since the badge is a glance-level cue; the actual
    per-currency figures below it are never ambiguous. Below
    PAYMENT_ROUNDING_TOLERANCE counts as settled, not debt — matching
    client_debts_by_currency's own treatment of that residue everywhere
    else in the app, so this badge can't disagree with the debt shown on
    the client's own page."""
    raw_sections = client_statement(client, date_from=date_from, date_to=date_to)
    sections = {cur: raw_sections[cur] for cur in CURRENCY_CODES if cur in raw_sections}

    tolerance = settings.PAYMENT_ROUNDING_TOLERANCE
    has_debt = any(s["closing"] > tolerance for s in sections.values())
    has_credit = any(s["closing"] < 0 for s in sections.values())
    status = "debt" if has_debt else "credit" if has_credit else "paid"

    return {
        "client": client,
        # Client-facing text uses first_name ONLY, never the staff-only
        # descriptor client.name parenthesises, and never the phone — same
        # rule as the receipt (see receipts.receipt_context).
        "client_name": client.first_name,
        "sections": sections,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
        "contact_line": settings.RECEIPT_CONTACT_LINE,
    }
