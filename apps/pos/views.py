"""Manager-facing sales terminal at /pos/. Server-rendered + HTMX partial
swaps — no API layer, no JS framework. Every screen calls the existing
apps/*/services.py functions; nothing here recomputes stock, money, or debt.
"""

import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from xhtml2pdf import pisa

from apps.clients.models import Client, Interaction
from apps.clients.services import client_debt
from apps.core.currency import (
    CENTS,
    CURRENCY_CODES,
    CURRENCY_SYMBOLS,
    get_rate,
    rate_info,
    to_base,
)
from apps.core.ratelimit import client_ip, rate_limit
from apps.inventory.cache import catalog_version
from apps.inventory.models import Product, ProductVariant, StockMovement
from apps.sales.models import Payment, SaleItem, SaleOrder
from apps.sales.services import (
    allocate_oldest_first,
    balance_kgs_before_payment,
    cancel_sale,
    compute_change_preview,
    confirm_sale,
    pay_oldest_first,
    record_payment,
    return_items,
    today_summary,
)

from . import undo
from .decorators import pos_view
from .messaging import debt_reminder_text, receipt_share_text, statement_share_text, wa_link
from .receipts import receipt_context
from .statements import statement_context

logger = logging.getLogger(__name__)

RECENT_CLIENTS_LIMIT = 10
PRODUCT_GRID_LIMIT = 24

_SW_SOURCE = settings.BASE_DIR / "static" / "pos" / "js" / "sw.js"


def service_worker(request):
    """Serves the service worker at /pos/sw.js rather than under /static/ —
    a service worker's default scope is its own directory, so this is the
    only URL that gives it control of all of /pos/ without needing a
    Service-Worker-Allowed header. No login required: it's static JS, not
    data. Cache-Control: no-cache so a deploy's cache-busting SHELL_VERSION
    bump (see the file itself) is picked up promptly, not held behind an
    HTTP cache on top of the browser's own SW update check."""
    content = Path(_SW_SOURCE).read_text(encoding="utf-8")
    response = HttpResponse(content, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    response["Service-Worker-Allowed"] = "/pos/"
    return response


def require_can_sell(view):
    """Editor+ only — Viewer never reaches a sale-editing endpoint, even by
    guessing the URL (the nav link is also hidden from them in the template)."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.has_perm("sales.add_saleorder"):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _parse_decimal(raw) -> Decimal | None:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return value if value >= 0 else None


def _parse_positive_decimal(raw) -> Decimal | None:
    value = _parse_decimal(raw)
    return value if value and value > 0 else None


def _parse_signed_decimal(raw) -> Decimal | None:
    """Unlike _parse_decimal, allows negative — needed for
    ClientOpeningBalance.amount, where negative is a legitimate «Аванс»
    (a client who'd pre-paid before go-live), never rejected as garbage."""
    try:
        return Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, AttributeError, ValueError):
        return None


def _parse_date(raw):
    """?from=YYYY-MM-DD / ?to=YYYY-MM-DD for the client statement's
    date-range filter — a malformed or absent value is simply "no filter
    on this end", never a 400/500. Never trust an unvalidated query string
    straight into a date-range DB filter."""
    from datetime import datetime

    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _check_historical_sale(request) -> tuple[bool, object]:
    """Backdating confirmed_at is Owner-only, enforced here regardless of
    what the UI shows — a non-superuser POSTing is_historical=1 (even
    directly, UI bypassed entirely) is rejected with a 403, never silently
    ignored, same pattern as _check_rate_override/_check_change_override.
    An unparseable/missing date comes back as (True, None) — the caller's
    job to turn that into a normal messages.error + redirect, same as
    every other pre-confirm validation failure in this view, never a raw
    500."""
    if request.POST.get("is_historical") != "1":
        return False, None
    if not request.user.is_superuser:
        raise PermissionDenied("Backdating a sale is Owner-only.")
    return True, _parse_date(request.POST.get("historical_date"))


def _check_rate_override(request) -> Decimal | None:
    """Hand-entering a rate is Owner-only, enforced here regardless of what
    the UI shows — a non-superuser POSTing rate_override (even directly, UI
    bypassed entirely) is rejected with a 403, never silently ignored."""
    raw = (request.POST.get("rate_override") or "").strip()
    if not raw:
        return None
    if not request.user.is_superuser:
        raise PermissionDenied("Rate override is Owner-only.")
    return _parse_positive_decimal(raw)


def _check_change_override(
    request, auto_change_amount, change_currency, currency, resolved_rate
) -> Decimal | None:
    """The computed change is read-only by default. A manual adjustment is
    allowed only within ± CHANGE_ROUNDING_STEP × 2 (physical-cash reality) for
    Editor/Manager — enforced here regardless of what the UI shows, so a
    crafted POST from a non-owner going wider gets 403. Anything wider is
    Owner-only, never silently clamped."""
    raw = (request.POST.get("change_amount_override") or "").strip()
    if not raw:
        return None
    value = _parse_decimal(raw)
    if value is None:
        return None
    if request.user.is_superuser:
        return value
    band = Decimal(settings.CHANGE_ROUNDING_STEP) * 2
    if change_currency != settings.CURRENCY and change_currency == currency and resolved_rate:
        band = band / resolved_rate
    if abs(value - (auto_change_amount or Decimal("0"))) > band:
        raise PermissionDenied("Change adjustment beyond the allowed band is Owner-only.")
    return value


def _own_draft_or_404(request, pk):
    return get_object_or_404(SaleOrder, pk=pk, created_by=request.user, status=SaleOrder.DRAFT)


def _rate_age_class(age_days: int | None) -> str:
    """fresh (0-1d) -> paid, aging (2-3d, NORMAL — NBKR skips weekends/
    holidays) -> partial, stale (4+d) -> debt. Reused by the Курс card badge
    and the payment risk check — one threshold, defined once."""
    if age_days is None:
        return "neutral"
    if age_days < settings.RATE_STALE_WARN_DAYS:
        return "paid"
    if age_days < settings.RATE_STALE_DAYS:
        return "partial"
    return "debt"


def _convert_with_rate(amount, from_currency, to_currency, from_rate, to_rate):
    """Amount in `from_currency` -> `to_currency`, using explicitly supplied
    rates (each "1 unit = rate units of base") rather than looking them up —
    lets a live Owner rate_override flow through the same math as a stored
    rate. None if a needed rate is missing."""
    if from_currency == to_currency:
        return Decimal(amount).quantize(CENTS, rounding=ROUND_HALF_UP)
    if from_currency == settings.CURRENCY:
        base = Decimal(amount)
    elif from_rate:
        base = Decimal(amount) * from_rate
    else:
        return None
    if to_currency == settings.CURRENCY:
        return base.quantize(CENTS, rounding=ROUND_HALF_UP)
    if not to_rate:
        return None
    return (base / to_rate).quantize(CENTS, rounding=ROUND_HALF_UP)


def _payment_conversion(order, amount, currency, rate_override=None):
    """Single source of truth for a payment's conversion math AND its risk —
    used by the live preview (recalc) and the actual confirm (sale_confirm)
    alike, so the two paths can never disagree about what's risky.

    Returns a dict covering: whether conversion is even needed (same_currency
    skips it entirely — order currency == payment currency uses rate 1.0),
    the rate/date/source used, the сом-equivalent math to display, whether a
    rate is missing (convert_failed — payment must not be saved), an optional
    deviation warning for a manual rate far from official, and the risk
    reasons (Russian) that require an explicit second confirmation."""
    today = timezone.localdate()
    same_currency = currency == order.currency
    result = {
        "same_currency": same_currency,
        "rate": None,
        "rate_date": None,
        "rate_source": None,
        "age_days": None,
        "age_class": "neutral",
        "converted_kgs": None,
        "paid_in_order": amount if same_currency else Decimal("0"),
        "convert_failed": False,
        "is_manual": rate_override is not None,
        "official_rate": None,
        "deviation_warning": None,
        "reasons": [],
        "requires_ack": False,
    }
    if same_currency or amount <= 0:
        return result

    if rate_override is not None:
        rate = rate_override
        result["rate_source"] = Payment.RATE_MANUAL
        official = get_rate(currency, today)
        result["official_rate"] = official
        if official:
            deviation = abs(rate_override - official) / official
            if deviation > settings.MANUAL_RATE_DEVIATION_WARN_PCT:
                result["deviation_warning"] = _(
                    "Курс сильно отличается от официального (НБКР %(o)s)."
                ) % {"o": official}
        result["reasons"].append(_("курс введён вручную"))
    else:
        info = rate_info(currency)
        if info is None:
            result["convert_failed"] = True
            return result
        rate = info["rate"]
        result["rate_date"] = info["date"]
        result["rate_source"] = info["source"]
        age_days = (today - info["date"]).days
        result["age_days"] = age_days
        result["age_class"] = _rate_age_class(age_days)
        if age_days >= settings.RATE_STALE_DAYS:
            result["reasons"].append(_("курс устарел (%(n)s дн.)") % {"n": age_days})

    result["rate"] = rate
    result["converted_kgs"] = _convert_with_rate(amount, currency, settings.CURRENCY, rate, None)

    order_rate = get_rate(order.currency, today)
    paid_in_order = _convert_with_rate(amount, currency, order.currency, rate, order_rate)
    if paid_in_order is None:
        result["convert_failed"] = True
        return result
    result["paid_in_order"] = paid_in_order

    if result["converted_kgs"] is not None and result["converted_kgs"] > Decimal(
        settings.LARGE_PAYMENT_THRESHOLD_KGS
    ):
        result["reasons"].append(
            _("крупная сумма — %(a)s %(c)s")
            % {"a": result["converted_kgs"], "c": settings.CURRENCY}
        )

    result["requires_ack"] = bool(result["reasons"])
    return result


def _change_rate_for(order, conv, today):
    """The rate change math bridges through: the payment's own frozen-worthy
    rate when it's foreign, else the order currency's own rate (trivially 1
    for a KGS order — only a real lookup for the rare non-KGS-order,
    same-currency-payment case) — never a rate the payment itself didn't use."""
    if conv["same_currency"]:
        return get_rate(order.currency, today) or Decimal("1")
    return conv["rate"] or Decimal("1")


def _draft_balance_kgs(order, today):
    """The still-DRAFT order's outstanding balance in KGS — order.total_kgs
    isn't frozen until confirm_sale runs, and a draft never has payments
    attached yet (they're only created at confirm time), so this is simply
    the live item total converted to KGS at a rate that's never None."""
    total = sum((i.line_total for i in order.items.all()), Decimal("0"))
    if order.currency == settings.CURRENCY:
        return total
    from apps.core.currency import snapshot_rate_to_base

    rate = snapshot_rate_to_base(order.currency, today)
    return (total * rate).quantize(CENTS, rounding=ROUND_HALF_UP)


def _sale_body_context(
    order,
    request=None,
    payment_amount=None,
    payment_currency=None,
    payment_method=None,
    error=None,
    rate_override=None,
    can_override_rate=False,
    excess_disposition=None,
    change_currency=None,
    change_amount_override=None,
    change_adjust_reason="",
):
    items = list(order.items.select_related("variant__product__category"))
    total = sum((i.line_total for i in items), Decimal("0"))
    total_discount = sum((i.discount_amount for i in items), Decimal("0"))
    today = timezone.localdate()
    total_conv = None
    if order.currency != settings.CURRENCY:
        total_conv = to_base(total, order.currency, today)

    currency = payment_currency or order.currency
    amount = payment_amount if payment_amount is not None else Decimal("0")
    excess_disposition = excess_disposition or Payment.DISPOSITION_NONE
    resolved_change_currency = change_currency or order.currency

    conv = _payment_conversion(order, amount, currency, rate_override=rate_override)
    paid_in_order = conv["paid_in_order"] or Decimal("0")

    # THE OVERPAYMENT FORK: never auto-pick what excess money means. Computed
    # whenever there's a real payment amount to preview against.
    change = None
    if not conv["convert_failed"] and amount > 0:
        change_rate = _change_rate_for(order, conv, today)
        change = compute_change_preview(
            order,
            amount,
            currency,
            change_rate,
            _draft_balance_kgs(order, today),
            change_currency=resolved_change_currency,
        )
        if change["has_excess"] and excess_disposition == Payment.DISPOSITION_CHANGE:
            if change_amount_override is not None:
                change["change_amount"] = change_amount_override  # preview only
            if change["change_amount_kgs"] > Decimal(settings.CHANGE_CONFIRM_THRESHOLD_KGS):
                conv["reasons"].append(
                    _("крупная сдача — %(a)s %(c)s")
                    % {"a": change["change_amount_kgs"], "c": settings.CURRENCY}
                )
            conv["requires_ack"] = bool(conv["reasons"])

    requires_disposition_choice = bool(
        change and change["has_excess"] and excess_disposition == Payment.DISPOSITION_NONE
    )

    # CORE RULE: what reduces the balance is the NET, not the gross entered
    # amount. Giving change (or nothing, when disposition is undecided/none)
    # counts only the net; 'debt'/'credit' count the FULL gross (the excess
    # then pools onto the client's other same-currency debt automatically).
    if change and excess_disposition == Payment.DISPOSITION_CHANGE and change["has_excess"]:
        order_rate = get_rate(order.currency, today)
        net_in_order_ccy = _convert_with_rate(
            change["net_applied_kgs"], settings.CURRENCY, order.currency, None, order_rate
        )
        paid_in_order = net_in_order_ccy if net_in_order_ccy is not None else paid_in_order

    balance = max(total - paid_in_order, Decimal("0"))
    balance_conv = None
    if order.currency != settings.CURRENCY:
        balance_conv = to_base(balance, order.currency, today)

    paid_display = paid_in_order  # in the order's currency
    # No status chip on an empty basket — payment_status_for(0, 0) reads as
    # "unpaid", which would wrongly badge an empty sale as a debt.
    status = SaleOrder.payment_status_for(total, paid_display) if total > 0 else None

    return {
        "order": order,
        "items": items,
        "error": error,
        "recalc_url": reverse("pos:recalc", args=[order.pk]),
        "total": total,
        "total_conv": total_conv,
        "total_discount": total_discount,
        "base_currency": settings.CURRENCY,
        "currencies": CURRENCY_CODES,
        "methods": Payment.METHOD_CHOICES,
        "payment_amount": payment_amount,
        "payment_currency": currency,
        "payment_method": payment_method or Payment.CASH,
        "same_currency": conv["same_currency"],
        "balance": balance,
        "balance_conv": balance_conv,
        "paid_display": paid_display,
        "status": status,
        "conv": conv,
        "can_override_rate": can_override_rate,
        "rate_override": rate_override,
        "entered_amount": amount,
        "entered_currency": currency,
        "change": change,
        "excess_disposition": excess_disposition,
        "requires_disposition_choice": requires_disposition_choice,
        "change_currency": resolved_change_currency,
        "change_amount_override": change_amount_override,
        "change_adjust_reason": change_adjust_reason,
        "has_client": order.client_id is not None,
        # Undo/redo availability for the cart toolbar. Optional `request` so
        # the few call sites that only render a fragment (and have no session
        # to read) still work — the buttons just render disabled there.
        **(
            undo.state(request, order)
            if request is not None
            else {"can_undo": False, "can_redo": False}
        ),
    }


def _debt_pay_context(
    order,
    request=None,
    payment_amount=None,
    payment_currency=None,
    payment_method=None,
    rate_override=None,
    can_override_rate=False,
    excess_disposition=None,
    change_currency=None,
    change_amount_override=None,
    change_adjust_reason="",
):
    """Same shape as _sale_body_context, but for repaying an already-
    CONFIRMED sale's balance («Погасить долг») instead of building a draft
    cart — no items, order.total is already frozen, and the pre-payment
    balance for the change/overpay preview is balance_kgs_before_payment
    (every prior payment already applied), never _draft_balance_kgs. Feeds
    the SAME templates/pos/partials/payment_panel.html as the draft cart —
    one payment panel, reused, never a second payment path."""
    today = timezone.localdate()
    total_conv = None
    if order.currency != settings.CURRENCY:
        total_conv = to_base(order.total, order.currency, today)

    currency = payment_currency or order.currency
    amount = payment_amount if payment_amount is not None else Decimal("0")
    excess_disposition = excess_disposition or Payment.DISPOSITION_NONE
    resolved_change_currency = change_currency or order.currency

    conv = _payment_conversion(order, amount, currency, rate_override=rate_override)

    change = None
    if not conv["convert_failed"] and amount > 0:
        change_rate = _change_rate_for(order, conv, today)
        change = compute_change_preview(
            order,
            amount,
            currency,
            change_rate,
            balance_kgs_before_payment(order),
            change_currency=resolved_change_currency,
        )
        if change["has_excess"] and excess_disposition == Payment.DISPOSITION_CHANGE:
            if change_amount_override is not None:
                change["change_amount"] = change_amount_override  # preview only
            if change["change_amount_kgs"] > Decimal(settings.CHANGE_CONFIRM_THRESHOLD_KGS):
                conv["reasons"].append(
                    _("крупная сдача — %(a)s %(c)s")
                    % {"a": change["change_amount_kgs"], "c": settings.CURRENCY}
                )
            conv["requires_ack"] = bool(conv["reasons"])

    requires_disposition_choice = bool(
        change and change["has_excess"] and excess_disposition == Payment.DISPOSITION_NONE
    )

    balance = order.balance  # outstanding BEFORE this preview payment
    balance_conv = None
    if order.currency != settings.CURRENCY:
        balance_conv = to_base(balance, order.currency, today)

    return {
        "order": order,
        "recalc_url": reverse("pos:debt_pay_recalc", args=[order.pk]),
        "confirm_url": reverse("pos:debt_pay_confirm", args=[order.pk]),
        "total": order.total,
        "total_conv": total_conv,
        "base_currency": settings.CURRENCY,
        "currencies": CURRENCY_CODES,
        "methods": Payment.METHOD_CHOICES,
        "payment_amount": payment_amount,
        "payment_currency": currency,
        "payment_method": payment_method or Payment.CASH,
        "balance": balance,
        "balance_conv": balance_conv,
        "full_pay_amount": balance,
        "allow_debt_shortcut": False,  # this whole screen IS "в долг" already
        "conv": conv,
        "can_override_rate": can_override_rate,
        "rate_override": rate_override,
        "entered_amount": amount,
        "entered_currency": currency,
        "change": change,
        "excess_disposition": excess_disposition,
        "requires_disposition_choice": requires_disposition_choice,
        "change_currency": resolved_change_currency,
        "change_amount_override": change_amount_override,
        "change_adjust_reason": change_adjust_reason,
        "has_client": True,  # a repayable sale always has a client (see the _or_404 helper)
    }


def _today_rates():
    """Today's «1 unit of X = N base currency» rates for the Курс card —
    display-only, plus the rate's own date/age so staleness is visible (never
    blocking — NBKR skips weekends/holidays, 2-3 day gaps are normal). Skips a
    currency silently when no rate has ever been recorded (fetch_rates hasn't
    run yet, or an owner hasn't set one) rather than showing a blank/zero."""
    today = timezone.localdate()
    rates = []
    for code in CURRENCY_CODES:
        if code == settings.CURRENCY:
            continue
        info = rate_info(code)
        if info is None:
            continue
        age_days = (today - info["date"]).days
        rates.append(
            {
                "currency": code,
                "symbol": CURRENCY_SYMBOLS.get(code, code),
                "rate": info["rate"],
                "base_symbol": CURRENCY_SYMBOLS.get(settings.CURRENCY, settings.CURRENCY),
                "date": info["date"],
                "age_days": age_days,
                "age_class": _rate_age_class(age_days),
                "stale": age_days >= settings.RATE_STALE_DAYS,
                "source": info["source"],
            }
        )
    return rates


@pos_view
@require_can_sell
@require_POST
def refresh_rates(request):
    """On-demand rate pull behind the «Обновить» button on the Курс card —
    Frankfurter's API, pinned to the NBKR provider (nbkr.kg itself blocks
    the production server's IP outright; Frankfurter doesn't). Fetches the
    latest rates for every currency independently and re-renders the rate
    strip. A network/validation failure for a currency (or all of them) just
    keeps its last known rate — same fail-soft contract as the daily fetch,
    never a 500. Allowed for Editor/Manager AND Owner (require_can_sell) —
    it only pulls the official number, unlike a manual override which is
    Owner-only."""
    from apps.core.management.commands.fetch_rates import fetch_frankfurter_rates

    _updated, _rejected, details = fetch_frankfurter_rates(changed_by=request.user)
    # Tapping «Обновить» during a network blip used to look identical to a
    # real refresh — nothing told the manager whether it actually worked, or
    # distinguished "already current" from "failed". Each currency gets its
    # own outcome now (see fetch_frankfurter_rates's docstring); only set on
    # the refresh path itself (never the plain page-load render), so it
    # appears exactly once, right after the tap.
    refresh_result = [
        {
            "currency": code,
            "symbol": CURRENCY_SYMBOLS.get(code, code),
            **info,
        }
        for code, info in details.items()
    ]
    return render(
        request,
        "pos/partials/rates.html",
        {
            "rates": _today_rates(),
            "refresh_result": refresh_result,
        },
    )


@pos_view
def rate_edit(request):
    """Owner-only manual-rate dialog opened from the Курс card. Pre-fills each
    non-base currency's current rate for editing. Hand-entering a rate is
    OWNER-ONLY (CLAUDE.md) — enforced here and again in rate_save, not just
    hidden in the UI. The useful path where NBKR is unreachable (it blocks some
    server IPs): the Owner sets the rate by hand without opening /panel/."""
    if not request.user.is_superuser:
        raise PermissionDenied
    rate_rows = []
    for code in CURRENCY_CODES:
        if code == settings.CURRENCY:
            continue
        current = get_rate(code)
        rate_rows.append(
            {
                "currency": code,
                "symbol": CURRENCY_SYMBOLS.get(code, code),
                "value": current if current is not None else "",
            }
        )
    return render(
        request,
        "pos/partials/rate_modal.html",
        {
            "rate_rows": rate_rows,
            "base_symbol": CURRENCY_SYMBOLS.get(settings.CURRENCY, settings.CURRENCY),
        },
    )


@pos_view
@require_POST
def rate_save(request):
    """Save Owner-entered rates as MANUAL overrides (one row per currency,
    overwritten in place), logging each ACTUAL change to RateChangeLog — the
    same audit trail the /panel/ ExchangeRate admin writes. Owner-only,
    enforced server-side. Returns the refreshed rate strip and closes the
    dialog (out-of-band empty #rate-modal)."""
    if not request.user.is_superuser:
        raise PermissionDenied
    from apps.core.models import ExchangeRate, RateChangeLog

    today = timezone.localdate()
    for code in CURRENCY_CODES:
        if code == settings.CURRENCY:
            continue
        raw = (request.POST.get(f"rate_{code}", "") or "").strip().replace(",", ".")
        if not raw:
            continue  # left blank — leave that currency's rate untouched
        value = _parse_decimal(raw)
        if value is None or value <= 0:
            continue  # ignore a non-positive/garbage entry rather than 500

        old = ExchangeRate.objects.filter(currency=code).values_list("rate", flat=True).first()
        ExchangeRate.objects.update_or_create(
            currency=code,
            defaults={"rate": value, "date": today, "source": ExchangeRate.MANUAL},
        )
        if old is None or old != value:
            RateChangeLog.objects.create(
                currency=code,
                old_rate=old,
                new_rate=value,
                source=ExchangeRate.MANUAL,
                changed_by=request.user,
            )
    return render(request, "pos/partials/rate_saved.html", {"rates": _today_rates()})


# ---- Sale draft lifecycle -------------------------------------------------


@pos_view
def index(request):
    """Land on an open draft — reuse the manager's most recent one so a
    dropped connection or accidental refresh never loses the basket. ?new=1
    forces a fresh draft (an intentional 'start over'). /pos/ is also
    LOGIN_REDIRECT_URL, so a Viewer (no sales.add_saleorder) lands here too —
    route them to Сегодня instead of a dead-end 403.

    CHANGED: no longer creates a SaleOrder just to land on it. A draft used to
    be created the instant this page was hit — including a bare visit with no
    intent to sell — leaving an empty "Ожидает / 0.00 / —" row in the Продажи
    admin list for anyone who ever opened /pos/. The draft is now created
    LAZILY, on the first real action (an item added or a client picked — see
    sale_new/_get_or_create_pending_draft below); opening this page with
    nothing to resume lands on the empty terminal instead, which creates
    nothing in the database by itself."""
    if not request.user.has_perm("sales.add_saleorder"):
        return redirect("pos:today")
    order = None
    if request.GET.get("new") != "1":
        order = (
            SaleOrder.objects.filter(created_by=request.user, status=SaleOrder.DRAFT)
            .order_by("-created_at")
            .first()
        )
    if order is not None:
        return redirect("pos:sale_detail", pk=order.pk)
    return redirect("pos:sale_new")


def _get_or_create_pending_draft(request) -> SaleOrder:
    """The ONE place a draft SaleOrder is created for a reason other than
    already being real work — called from item_add_new/client_set_new/
    client_create_new below, the first time any of them fires for a given
    empty-terminal visit.

    Race-safe against a double-tap: sale_new (GET) mints a token into the
    session BEFORE either of two near-simultaneous first actions can fire, so
    both POSTs read the SAME token and race on THIS field's unique
    constraint via get_or_create — Django's own documented safe pattern for
    exactly this (the loser's IntegrityError makes it retry the SELECT and
    return the winner's row), contingent on a real DB constraint backing it,
    which SaleOrder.pending_token's unique=True provides. Two tabs opened
    independently naturally get two different orders: whichever acts first
    consumes and clears the shared token, so a second tab that acts later
    mints its own fresh one rather than reattaching to the first tab's
    now-real sale."""
    token = request.session.get("pending_sale_token")
    if not token:
        token = uuid.uuid4().hex
        request.session["pending_sale_token"] = token
    with transaction.atomic():
        order, _created = SaleOrder.objects.get_or_create(
            created_by=request.user,
            pending_token=token,
            defaults={"channel": SaleOrder.SHOP},
        )
    if request.session.get("pending_sale_token") == token:
        del request.session["pending_sale_token"]
        request.session.modified = True
    return order


@pos_view
@require_can_sell
def sale_new(request):
    """The empty terminal: no SaleOrder exists yet, and loading this page
    must not create one — see index()'s docstring. Renders the same Курс/
    Клиент/Товары layout as sale_detail, but every action here creates the
    draft lazily on first use (see _get_or_create_pending_draft) and then
    hands off entirely to the ordinary, unmodified per-order screen and
    endpoints — this page and its "_new" partials only ever represent the
    state BEFORE any order exists."""
    if "pending_sale_token" not in request.session:
        request.session["pending_sale_token"] = uuid.uuid4().hex
    return render(request, "pos/sale_new.html", {"active": "sale", "rates": _today_rates()})


@rate_limit("search", 300, 60)
@pos_view
@require_can_sell
def client_search_new(request):
    """Same search client_search does — read-only, so it needs no draft to
    run against. A near-duplicate of that view rather than a shared helper:
    the two differ only in which template they hand the results to (this one
    points "Выбрать" at client_set_new), and the query itself is three lines."""
    q = request.GET.get("q", "").strip()
    if q:
        clients = Client.objects.filter(
            Q(phone__icontains=q) | Q(first_name__icontains=q) | Q(descriptor__icontains=q)
        ).order_by("first_name")[:RECENT_CLIENTS_LIMIT]
    else:
        clients = Client.objects.order_by("-created_at")[:RECENT_CLIENTS_LIMIT]
    return render(request, "pos/partials/client_results_new.html", {"clients": clients, "q": q})


@pos_view
@require_can_sell
def client_new_form_new(request):
    """Opens the new-client form before any order exists — mirrors
    client_new_form, minus the order it doesn't have yet."""
    return render(request, "pos/partials/client_section_new.html", {"new_client_open": True})


@pos_view
@require_can_sell
def client_section_reset_new(request):
    """Back to the search state from the new-client form's «Отмена» — pure UI
    state, no order to clear (there isn't one yet), so unlike client_clear
    this needs no POST/mutation at all."""
    return render(request, "pos/partials/client_section_new.html", {})


@rate_limit("search", 300, 60)
@pos_view
@require_can_sell
def product_grid_new(request):
    """Same tile grid as product_grid — _build_grid_tiles is already
    order-independent (its own docstring says so), so this just skips the
    _own_draft_or_404 lookup product_grid does purely to pass `order` through
    for URL-building, and points tiles at variant_picker_new instead."""
    q = request.GET.get("q", "").strip()
    cache_key = f"grid:v{catalog_version()}:{q.lower()}"
    tiles = cache.get(cache_key)
    if tiles is None:
        tiles = _build_grid_tiles(q)
        cache.set(cache_key, tiles, 60)
    return render(request, "pos/partials/product_grid_new.html", {"tiles": tiles, "q": q})


@pos_view
@require_can_sell
def variant_picker_new(request, product_id):
    """variant_picker before any order exists: `already_in_cart` is always
    empty here by construction (nothing has been added to a cart that isn't
    real yet), so available_qty is simply on_hand minus reserved."""
    from apps.inventory.services import reserved_by_variant

    product = get_object_or_404(Product.objects.select_related("category"), pk=product_id)
    variants = list(
        product.variants.filter(is_active=True).annotate(stock_qty=Sum("movements__quantity"))
    )
    reserved = reserved_by_variant(variant_ids=[v.pk for v in variants])
    for v in variants:
        on_hand = v.stock_qty or 0
        v.reserved_qty = reserved.get(v.pk, 0)
        v.available_qty = max(on_hand - v.reserved_qty, 0)
    return render(
        request, "pos/partials/variant_picker_new.html", {"product": product, "variants": variants}
    )


@pos_view
@require_can_sell
@require_POST
def item_add_new(request):
    """Creates the draft lazily, then hands off ENTIRELY to the existing,
    unmodified item_add for the real work — this changes nothing about what
    item_add does or how (its own stock-cap/currency logic is untouched).
    Always lands on the real per-order page afterward via HX-Redirect (a full
    client-side navigation, not a partial swap — sale_new.html has no
    #sale-body for item_add's normal response to swap into): the
    currency-mismatch branch can't fire on a brand-new empty order (it only
    fires once items already exist), and a stock-cap clamp still leaves a
    correct, real cart behind even though its one-shot warning message
    doesn't survive the redirect — no worse than an ordinary page reload
    already not preserving that same message today."""
    order = _get_or_create_pending_draft(request)
    item_add(request, order.pk)
    response = HttpResponse(status=200)
    response["HX-Redirect"] = reverse("pos:sale_detail", args=[order.pk])
    return response


@pos_view
@require_can_sell
@require_POST
def client_set_new(request, client_id):
    """Same hand-off pattern as item_add_new. client_set has no failure path
    of its own (get_object_or_404 either finds the client or 404s — no
    in-app validation branch), so redirecting unconditionally loses nothing."""
    order = _get_or_create_pending_draft(request)
    client_set(request, order.pk, client_id)
    response = HttpResponse(status=200)
    response["HX-Redirect"] = reverse("pos:sale_detail", args=[order.pk])
    return response


@pos_view
@require_can_sell
@require_POST
def client_create_new(request):
    """Unlike the two above, client_create has a real validation path
    (required fields, a duplicate phone) worth preserving inline rather than
    losing across a redirect — a mistyped phone number bouncing to a
    freshly-created, client-less order would be a real step backward.
    On success (order.client_id got set), hand off exactly like item_add_new/
    client_set_new. On failure, return client_create's OWN error partial as
    normal htmx content (no HX-Redirect) — order.pk is real by now, so every
    URL inside that partial, including a retry, already points at the
    ordinary, unmodified client_create and keeps working without any further
    help from this wrapper."""
    order = _get_or_create_pending_draft(request)
    response = client_create(request, order.pk)
    order.refresh_from_db()
    if order.client_id:
        response = HttpResponse(status=200)
        response["HX-Redirect"] = reverse("pos:sale_detail", args=[order.pk])
    return response


@pos_view
@require_can_sell
def sale_detail(request, pk):
    order = get_object_or_404(SaleOrder, pk=pk, created_by=request.user)
    if order.status != SaleOrder.DRAFT:
        return redirect("pos:sale_result", pk=order.pk)
    context = {
        **_sale_body_context(order, request, can_override_rate=request.user.is_superuser),
        "active": "sale",
        "rates": _today_rates(),
    }
    return render(request, "pos/sale_detail.html", context)


# ---- Client section (HTMX partials) ---------------------------------------


@rate_limit("search", 300, 60)
@pos_view
@require_can_sell
def client_search(request, pk):
    order = _own_draft_or_404(request, pk)
    q = request.GET.get("q", "").strip()
    if q:
        clients = Client.objects.filter(
            Q(phone__icontains=q) | Q(first_name__icontains=q) | Q(descriptor__icontains=q)
        ).order_by("first_name")[:RECENT_CLIENTS_LIMIT]
    else:
        clients = Client.objects.order_by("-created_at")[:RECENT_CLIENTS_LIMIT]
    return render(
        request, "pos/partials/client_results.html", {"order": order, "clients": clients, "q": q}
    )


@pos_view
@require_can_sell
@require_POST
def client_set(request, pk, client_id):
    order = _own_draft_or_404(request, pk)
    undo.push(request, order)
    order.client = get_object_or_404(Client, pk=client_id)
    order.save(update_fields=["client"])
    return render(request, "pos/partials/client_section.html", {"order": order})


@pos_view
@require_can_sell
@require_POST
def client_clear(request, pk):
    order = _own_draft_or_404(request, pk)
    undo.push(request, order)
    order.client = None
    order.save(update_fields=["client"])
    return render(request, "pos/partials/client_section.html", {"order": order})


@pos_view
@require_can_sell
def client_new_form(request, pk):
    order = _own_draft_or_404(request, pk)
    return render(
        request, "pos/partials/client_section.html", {"order": order, "new_client_open": True}
    )


@pos_view
@require_can_sell
@require_POST
def client_create(request, pk):
    order = _own_draft_or_404(request, pk)
    first_name = request.POST.get("first_name", "").strip()
    descriptor = request.POST.get("descriptor", "").strip()
    phone = request.POST.get("phone", "").strip()

    error = None
    if not first_name or not phone:
        error = _("Имя и телефон обязательны.")
    elif Client.objects.filter(phone=phone).exists():
        error = _("Клиент с этим телефоном уже есть.")
    if error:
        return render(
            request,
            "pos/partials/client_section.html",
            {
                "order": order,
                "new_client_open": True,
                "new_client_error": error,
                "new_client_values": {
                    "first_name": first_name,
                    "descriptor": descriptor,
                    "phone": phone,
                },
            },
        )

    client = Client.objects.create(
        first_name=first_name, descriptor=descriptor, phone=phone, source=Client.SHOP
    )
    # Pushed only now that the new client actually exists: an undo step for a
    # validation bounce above would be a step that changed nothing. Undo
    # detaches the client from THIS sale — it never deletes the Client record,
    # which by then may already be referenced elsewhere.
    undo.push(request, order)
    order.client = client
    order.save(update_fields=["client"])
    return render(request, "pos/partials/client_section.html", {"order": order})


# ---- Product grid + line items (HTMX partials) -----------------------------


def _build_grid_tiles(q: str) -> list[dict]:
    """The grid's catalog data (order-independent) — exactly TWO queries and a
    constant count no matter how many products exist in the DB: the products are
    LIMIT-bounded, and their variants are fetched in a single grouped query
    instead of one-per-product. Stock and reservation both come from correlated
    subqueries so the SKU-search join can't fan out and double-count them.
    Returns plain dicts so the result caches cleanly and renders per-request
    against the current draft.

    `stock` is AVAILABLE (on_hand − reserved), never raw on_hand — units
    promised to an open production order (apps.orders) must not look sellable
    on the tile, matching the confirm-time guard in services.confirm_sale."""
    from apps.orders.models import Order, OrderItem

    stock_subquery = Subquery(
        StockMovement.objects.filter(variant__product=OuterRef("pk"))
        .values("variant__product")
        .annotate(s=Sum("quantity"))
        .values("s"),
        output_field=IntegerField(),
    )
    reserved_subquery = Subquery(
        OrderItem.objects.filter(
            variant__product=OuterRef("pk"),
            order__status__in=[Order.NEW, Order.IN_PRODUCTION, Order.READY],
        )
        .values("variant__product")
        .annotate(r=Sum("quantity"))
        .values("r"),
        output_field=IntegerField(),
    )
    # prefetch_related("images"), not select_related — a product can have many
    # now, so this is ONE extra query for the whole grid (Meta.ordering on
    # ProductImage already sorts it cover-first), not N. Product.grid_image
    # reads from this cache via `self.images.all()` rather than `.first()` —
    # see its docstring for why that distinction is what keeps this O(1).
    products = (
        Product.objects.filter(is_active=True).select_related("category").prefetch_related("images")
    )
    if q:
        products = products.filter(Q(name__icontains=q) | Q(variants__sku__icontains=q)).distinct()
    products = list(
        products.annotate(
            _stock=Coalesce(stock_subquery, 0), _reserved=Coalesce(reserved_subquery, 0)
        ).order_by("name")[:PRODUCT_GRID_LIMIT]
    )

    ids = [p.pk for p in products]
    variants_by_product: dict[int, list] = {pid: [] for pid in ids}
    for pid, threshold, price, currency in ProductVariant.objects.filter(
        is_active=True, product_id__in=ids
    ).values_list("product_id", "low_stock_threshold", "sale_price", "currency"):
        variants_by_product[pid].append((threshold, price, currency))

    tiles = []
    for p in products:
        variants = variants_by_product.get(p.pk, [])
        min_threshold = min((v[0] for v in variants), default=0)
        on_hand = p._stock or 0
        reserved = p._reserved or 0
        stock = max(on_hand - reserved, 0)
        currencies_used = {v[2] for v in variants}
        # A "from X" price only means something when every variant is priced in
        # the SAME currency — comparing raw numbers across currencies (80 USD vs
        # 2500 KGS) would pick the wrong "cheapest", the exact cross-currency
        # mixup the app must never do.
        if len(currencies_used) == 1:
            cheapest = min(variants, key=lambda v: v[1])
            price, price_currency = cheapest[1], cheapest[2]
            price_varies = len({v[1] for v in variants}) > 1
        else:
            price, price_currency, price_varies = None, None, False
        image = p.grid_image
        tiles.append(
            {
                "product_id": p.pk,
                "name": p.name,
                "category": p.category.name,
                "image_url": image.url if image else "",
                "stock": stock,
                "reserved": reserved,
                "low": 0 < stock <= min_threshold,
                "out": stock <= 0,
                "price": price,
                "currency": price_currency,
                "price_varies": price_varies,
            }
        )
    return tiles


@rate_limit("search", 300, 60)
@pos_view
@require_can_sell
def product_grid(request, pk):
    order = _own_draft_or_404(request, pk)
    q = request.GET.get("q", "").strip()
    # Cache the catalog data (not the HTML — the HTML embeds this draft's URLs).
    # Key includes the catalog version, so any stock/product/variant write makes
    # it instantly stale (see inventory/signals.py). 60s TTL as a backstop.
    cache_key = f"grid:v{catalog_version()}:{q.lower()}"
    tiles = cache.get(cache_key)
    if tiles is None:
        tiles = _build_grid_tiles(q)
        cache.set(cache_key, tiles, 60)
    return render(
        request, "pos/partials/product_grid.html", {"order": order, "tiles": tiles, "q": q}
    )


@pos_view
@require_can_sell
def variant_picker(request, pk, product_id):
    from apps.inventory.services import reserved_by_variant

    order = _own_draft_or_404(request, pk)
    product = get_object_or_404(Product.objects.select_related("category"), pk=product_id)
    variants = list(
        product.variants.filter(is_active=True).annotate(stock_qty=Sum("movements__quantity"))
    )
    reserved = reserved_by_variant(variant_ids=[v.pk for v in variants])
    already_in_cart = {
        row["variant_id"]: row["s"] or 0
        for row in order.items.filter(variant__in=variants)
        .values("variant_id")
        .annotate(s=Sum("quantity"))
    }
    for v in variants:
        on_hand = v.stock_qty or 0
        v.reserved_qty = reserved.get(v.pk, 0)
        # What's left to add to THIS draft — available minus whatever of this
        # variant is already sitting in the cart (Part 1a: capped across ALL
        # lines, not per line), so the picker can never offer more than the
        # server will actually accept.
        v.available_qty = max(on_hand - v.reserved_qty - already_in_cart.get(v.pk, 0), 0)
    return render(
        request,
        "pos/partials/variant_picker.html",
        {"order": order, "product": product, "variants": variants},
    )


@pos_view
@require_can_sell
@require_POST
def item_add(request, pk):
    from apps.inventory.services import available_for

    order = _own_draft_or_404(request, pk)
    # A missing/blank variant_id reached the ORM as "" and raised ValueError —
    # a 500 on malformed input, where a 404 is the honest answer. Parsed here
    # so the pk is an int (or nothing) before it ever gets to the query.
    try:
        variant_id = int(request.POST.get("variant_id") or "")
    except (TypeError, ValueError):
        raise Http404("No variant given.") from None
    undo.push(request, order)
    variant = get_object_or_404(ProductVariant, pk=variant_id)
    try:
        qty = max(int(request.POST.get("quantity", "1")), 1)
    except (TypeError, ValueError):
        qty = 1

    # SaleItem has no currency of its own — unit_price is implicitly stored in
    # order.currency. An empty draft adopts the first item's currency; once
    # items exist, a mismatched variant would silently mis-price (e.g. a USD
    # price read as KGS), so it's rejected instead of guessed at.
    if not order.items.exists():
        if order.currency != variant.currency:
            order.currency = variant.currency
            order.save(update_fields=["currency"])
    elif order.currency != variant.currency:
        error = _(
            "Товар в %(vc)s нельзя добавить в продажу в %(oc)s — оформите отдельной продажей."
        ) % {"vc": variant.currency, "oc": order.currency}
        return render(
            request,
            "pos/partials/sale_body.html",
            _sale_body_context(
                order, request, error=error, can_override_rate=request.user.is_superuser
            ),
        )

    # CART-TIME CAP (Part 1a): capped against the TOTAL of this variant across
    # ALL lines on this draft, never per line — a client-side max= is UX only,
    # the server clamps and explains regardless (Part 1c/1d).
    available = available_for(variant)
    already_in_cart = order.items.filter(variant=variant).aggregate(s=Sum("quantity"))["s"] or 0
    room = max(available - already_in_cart, 0)
    error = None
    if room <= 0:
        error = _("Товар «%(name)s» уже полностью в корзине — доступно %(n)s шт.") % {
            "name": str(variant),
            "n": max(available, 0),
        }
        qty = 0
    elif qty > room:
        error = _("Доступно только %(n)s шт «%(name)s».") % {"n": available, "name": str(variant)}
        qty = room

    if qty > 0:
        existing = order.items.filter(variant=variant).first()
        if existing:
            existing.quantity += qty
            existing.save(update_fields=["quantity"])
        else:
            SaleItem.objects.create(
                order=order, variant=variant, quantity=qty, unit_price=variant.sale_price
            )
    return render(
        request,
        "pos/partials/sale_body.html",
        _sale_body_context(
            order, request, error=error, can_override_rate=request.user.is_superuser
        ),
    )


@pos_view
@require_can_sell
@require_POST
def item_remove(request, pk, item_id):
    order = _own_draft_or_404(request, pk)
    undo.push(request, order)
    order.items.filter(pk=item_id).delete()
    return render(
        request,
        "pos/partials/sale_body.html",
        _sale_body_context(order, request, can_override_rate=request.user.is_superuser),
    )


@pos_view
@require_can_sell
@require_POST
def sale_undo(request, pk):
    """Step the draft back one edit (see apps.pos.undo). Both the client
    section and the cart re-render, because an undo can move the client as
    well as the lines — client_section is swapped out-of-band from
    sale_body.html so one response updates both."""
    order = _own_draft_or_404(request, pk)
    if not undo.undo(request, order):
        return render(
            request,
            "pos/partials/sale_body.html",
            _sale_body_context(
                order,
                request,
                error=_("Отменять больше нечего."),
                can_override_rate=request.user.is_superuser,
            ),
        )
    order.refresh_from_db()
    return render(
        request,
        "pos/partials/sale_body.html",
        _sale_body_context(order, request, can_override_rate=request.user.is_superuser),
    )


@pos_view
@require_can_sell
@require_POST
def sale_redo(request, pk):
    order = _own_draft_or_404(request, pk)
    if not undo.redo(request, order):
        return render(
            request,
            "pos/partials/sale_body.html",
            _sale_body_context(
                order,
                request,
                error=_("Возвращать больше нечего."),
                can_override_rate=request.user.is_superuser,
            ),
        )
    order.refresh_from_db()
    return render(
        request,
        "pos/partials/sale_body.html",
        _sale_body_context(order, request, can_override_rate=request.user.is_superuser),
    )


@pos_view
@require_can_sell
@require_POST
def item_discount(request, pk, item_id):
    """Set (or clear) one cart line's discount. Never trusts the client-side
    max= alone (the cart-time-stock-cap precedent, CLAUDE.md Part 1c/1d):
    a percent is clamped to 0-100, a fixed amount to 0-subtotal, here, again,
    regardless of what was actually POSTed — the same two bounds the DB
    CheckConstraints enforce as the last line of defence. discount_type=none
    always zeroes discount_value out too, rather than leaving a stale value
    inert-but-present from a previous discount."""
    order = _own_draft_or_404(request, pk)
    undo.push(request, order)
    item = get_object_or_404(SaleItem, pk=item_id, order=order)

    discount_type = request.POST.get("discount_type", SaleItem.DISCOUNT_NONE)
    if discount_type not in dict(SaleItem.DISCOUNT_TYPE_CHOICES):
        discount_type = SaleItem.DISCOUNT_NONE

    raw_value = _parse_decimal(request.POST.get("discount_value", "")) or Decimal("0")
    subtotal = item.unit_price * item.quantity
    if discount_type == SaleItem.DISCOUNT_PERCENT:
        value = min(raw_value, Decimal("100"))
    elif discount_type == SaleItem.DISCOUNT_FIXED:
        value = min(raw_value, subtotal)
    else:
        value = Decimal("0")

    item.discount_type = discount_type
    item.discount_value = value
    item.save(update_fields=["discount_type", "discount_value"])
    return render(
        request,
        "pos/partials/sale_body.html",
        _sale_body_context(order, request, can_override_rate=request.user.is_superuser),
    )


@pos_view
@require_can_sell
@require_POST
def recalc(request, pk):
    """Live Итого/Оплачено/Остаток preview as payment fields change — pure
    display, nothing persisted until confirm."""
    order = _own_draft_or_404(request, pk)
    amount = _parse_decimal(request.POST.get("amount")) or Decimal("0")
    currency = request.POST.get("currency") or order.currency
    method = request.POST.get("method") or Payment.CASH
    rate_override = _check_rate_override(request)
    excess_disposition = request.POST.get("excess_disposition") or None
    change_currency = request.POST.get("change_currency") or None

    change_amount_override = None
    if excess_disposition == Payment.DISPOSITION_CHANGE and amount > 0:
        conv_preview = _payment_conversion(order, amount, currency, rate_override=rate_override)
        if not conv_preview["convert_failed"]:
            resolved_change_currency = change_currency or order.currency
            today = timezone.localdate()
            change_rate = _change_rate_for(order, conv_preview, today)
            preview = compute_change_preview(
                order,
                amount,
                currency,
                change_rate,
                _draft_balance_kgs(order, today),
                change_currency=resolved_change_currency,
            )
            change_amount_override = _check_change_override(
                request,
                preview["change_amount"],
                resolved_change_currency,
                currency,
                change_rate,
            )

    return render(
        request,
        "pos/partials/sale_body.html",
        _sale_body_context(
            order,
            request,
            payment_amount=amount,
            payment_currency=currency,
            payment_method=method,
            rate_override=rate_override,
            can_override_rate=request.user.is_superuser,
            excess_disposition=excess_disposition,
            change_currency=change_currency,
            change_amount_override=change_amount_override,
            change_adjust_reason=request.POST.get("change_adjust_reason", ""),
        ),
    )


# ---- Confirm / result / cancel ---------------------------------------------


@pos_view
@require_can_sell
@require_POST
def sale_confirm(request, pk):
    order = get_object_or_404(SaleOrder, pk=pk, created_by=request.user)
    if order.status != SaleOrder.DRAFT:
        # Already confirmed — most likely a double-tap. Idempotent: show the
        # existing result instead of erroring or creating a second sale.
        return redirect("pos:sale_result", pk=order.pk)

    is_historical, historical_date = _check_historical_sale(request)  # 403s if not Owner
    if is_historical and historical_date is None:
        messages.error(request, _("Укажите дату исторической продажи."))
        return redirect("pos:sale_detail", pk=order.pk)

    amount = _parse_decimal(request.POST.get("amount"))
    currency = request.POST.get("currency") or order.currency
    method = request.POST.get("method") or Payment.CASH
    rate_override = _check_rate_override(request)  # raises PermissionDenied if not Owner
    excess_disposition = request.POST.get("excess_disposition") or Payment.DISPOSITION_NONE
    change_currency = request.POST.get("change_currency") or order.currency
    change_amount_override = None

    # Validate the payment's conversion/risk BEFORE touching stock: a missing
    # rate or an un-acknowledged risky payment must never leave the order
    # half-confirmed with no payment recorded behind it.
    if amount and amount > 0:
        conv = _payment_conversion(order, amount, currency, rate_override=rate_override)
        if conv["convert_failed"]:
            messages.error(
                request,
                _("Нет курса для %(c)s — оплата не сохранена. Обновите курс и повторите.")
                % {"c": currency},
            )
            return redirect("pos:sale_detail", pk=order.pk)

        today = timezone.localdate()
        change_rate = _change_rate_for(order, conv, today)
        preview = compute_change_preview(
            order,
            amount,
            currency,
            change_rate,
            _draft_balance_kgs(order, today),
            change_currency=change_currency,
        )

        # THE OVERPAYMENT FORK: an excess with no explicit choice is rejected
        # — never auto-picked.
        if preview["has_excess"] and excess_disposition == Payment.DISPOSITION_NONE:
            messages.error(
                request,
                _("Выберите, что сделать с излишком: сдача, в счёт долга или аванс."),
            )
            return redirect("pos:sale_detail", pk=order.pk)

        if excess_disposition in (Payment.DISPOSITION_DEBT, Payment.DISPOSITION_CREDIT):
            if order.client_id is None:
                messages.error(request, _("«В счёт долга» и «Аванс» требуют клиента на продаже."))
                return redirect("pos:sale_detail", pk=order.pk)

        if excess_disposition == Payment.DISPOSITION_CHANGE and preview["has_excess"]:
            # A manual adjustment beyond the band raises PermissionDenied (403)
            # for anyone but the Owner — never silently clamped.
            change_amount_override = _check_change_override(
                request, preview["change_amount"], change_currency, currency, change_rate
            )
            if preview["change_amount_kgs"] > Decimal(settings.CHANGE_CONFIRM_THRESHOLD_KGS):
                conv["reasons"].append(
                    _("крупная сдача — %(a)s %(c)s")
                    % {"a": preview["change_amount_kgs"], "c": settings.CURRENCY}
                )
                conv["requires_ack"] = True

        if conv["requires_ack"] and request.POST.get("risk_ack") != "1":
            messages.error(
                request,
                _("Требуется подтверждение платежа: %(reasons)s")
                % {"reasons": "; ".join(conv["reasons"])},
            )
            return redirect("pos:sale_detail", pk=order.pk)

    try:
        confirm_sale(
            order,
            user=request.user,
            is_historical=is_historical,
            historical_date=historical_date,
        )
    except ValidationError as exc:
        order.refresh_from_db()
        if order.status == SaleOrder.CONFIRMED:
            # Lost a race to a concurrent confirm of the same order — still idempotent.
            return redirect("pos:sale_result", pk=order.pk)
        # Redirect back to a GET-able page rather than rendering directly: a
        # refresh after an oversell must not prompt "resubmit form?", and the
        # offline-safe fetch() submit in confirm.js needs every outcome of
        # this POST to end in a redirect it can safely follow and navigate to.
        messages.error(request, "; ".join(exc.messages))
        return redirect("pos:sale_detail", pk=order.pk)

    if amount and amount > 0:
        try:
            record_payment(
                order,
                amount,
                user=request.user,
                method=method,
                currency=currency,
                rate_override=rate_override,
                excess_disposition=excess_disposition,
                change_currency=change_currency,
                change_amount_override=change_amount_override,
                change_adjust_reason=request.POST.get("change_adjust_reason", ""),
            )
        except ValidationError as exc:
            # Belt-and-suspenders: the pre-check above should already have
            # caught this (e.g. a rate vanished in the race between the check
            # and here) — the sale itself still stands, only the payment
            # didn't save, and that's surfaced clearly rather than silently.
            messages.error(request, "; ".join(exc.messages))
    # The sale is committed — its undo history goes with the draft it belonged
    # to. From here the way back is «Отменить продажу»/«Оформить возврат»,
    # which restock through services.py and leave a ledger trail.
    undo.clear(request, order.pk)
    return redirect("pos:sale_result", pk=order.pk)


# ---- Debt repayment ("Погасить долг") --------------------------------------


def _payable_sale_or_404(request, pk):
    return get_object_or_404(SaleOrder, pk=pk, status=SaleOrder.CONFIRMED, client__isnull=False)


@pos_view
@require_can_sell
def debt_pay_detail(request, pk):
    """«Погасить долг» from a client's purchase history — the SAME payment
    panel used everywhere (frozen rate, cross-currency double-check), against
    an already-CONFIRMED sale's outstanding balance. Client is fixed (the
    sale's own); amount defaults to the remaining balance but partial is
    allowed, exactly like any other payment."""
    order = _payable_sale_or_404(request, pk)
    context = _debt_pay_context(
        order,
        request,
        # Pre-filled with the remaining balance — partial is still
        # allowed, just edit the field before submitting.
        payment_amount=order.balance,
        payment_currency=order.currency,
        can_override_rate=request.user.is_superuser,
    )
    # The screen used to show only money — no reminder of WHAT was bought.
    # Rendered once here (not inside _debt_pay_context, which recalc_url's
    # HTMX preview re-runs on every keystroke) since the item list never
    # changes while the payment form is being filled in.
    context["order_items"] = order.items.select_related("variant__product__category").order_by(
        "variant__product__name", "variant__size", "variant__color"
    )
    context["can_resend_receipt"] = bool(
        order.client and order.client.phone and request.user.has_perm("clients.add_interaction")
    )
    return render(request, "pos/debt_pay.html", context)


@pos_view
@require_can_sell
@require_POST
def debt_pay_recalc(request, pk):
    """Live double-check preview as the repayment form changes — mirrors
    apps.pos.views.recalc exactly, just against balance_kgs_before_payment
    instead of a draft's live item total."""
    order = _payable_sale_or_404(request, pk)
    amount = _parse_decimal(request.POST.get("amount")) or Decimal("0")
    currency = request.POST.get("currency") or order.currency
    method = request.POST.get("method") or Payment.CASH
    rate_override = _check_rate_override(request)
    excess_disposition = request.POST.get("excess_disposition") or None
    change_currency = request.POST.get("change_currency") or None

    change_amount_override = None
    if excess_disposition == Payment.DISPOSITION_CHANGE and amount > 0:
        conv_preview = _payment_conversion(order, amount, currency, rate_override=rate_override)
        if not conv_preview["convert_failed"]:
            resolved_change_currency = change_currency or order.currency
            change_rate = _change_rate_for(order, conv_preview, timezone.localdate())
            preview = compute_change_preview(
                order,
                amount,
                currency,
                change_rate,
                balance_kgs_before_payment(order),
                change_currency=resolved_change_currency,
            )
            change_amount_override = _check_change_override(
                request,
                preview["change_amount"],
                resolved_change_currency,
                currency,
                change_rate,
            )

    return render(
        request,
        "pos/partials/debt_pay_body.html",
        _debt_pay_context(
            order,
            request,
            payment_amount=amount,
            payment_currency=currency,
            payment_method=method,
            rate_override=rate_override,
            can_override_rate=request.user.is_superuser,
            excess_disposition=excess_disposition,
            change_currency=change_currency,
            change_amount_override=change_amount_override,
            change_adjust_reason=request.POST.get("change_adjust_reason", ""),
        ),
    )


@pos_view
@require_can_sell
@require_POST
def debt_pay_confirm(request, pk):
    """Records the repayment via the EXISTING services.record_payment — same
    risk/overpayment/change rules as confirming a sale, just with no stock or
    items involved (the sale is already confirmed)."""
    order = _payable_sale_or_404(request, pk)
    amount = _parse_decimal(request.POST.get("amount"))
    currency = request.POST.get("currency") or order.currency
    method = request.POST.get("method") or Payment.CASH
    rate_override = _check_rate_override(request)
    excess_disposition = request.POST.get("excess_disposition") or Payment.DISPOSITION_NONE
    change_currency = request.POST.get("change_currency") or order.currency

    if not amount or amount <= 0:
        messages.error(request, _("Укажите сумму больше нуля."))
        return redirect("pos:debt_pay_detail", pk=order.pk)

    conv = _payment_conversion(order, amount, currency, rate_override=rate_override)
    if conv["convert_failed"]:
        messages.error(
            request,
            _("Нет курса для %(c)s — оплата не сохранена. Обновите курс и повторите.")
            % {"c": currency},
        )
        return redirect("pos:debt_pay_detail", pk=order.pk)

    today = timezone.localdate()
    change_rate = _change_rate_for(order, conv, today)
    preview = compute_change_preview(
        order,
        amount,
        currency,
        change_rate,
        balance_kgs_before_payment(order),
        change_currency=change_currency,
    )

    if preview["has_excess"] and excess_disposition == Payment.DISPOSITION_NONE:
        messages.error(
            request, _("Выберите, что сделать с излишком: сдача, в счёт долга или аванс.")
        )
        return redirect("pos:debt_pay_detail", pk=order.pk)

    change_amount_override = None
    if excess_disposition == Payment.DISPOSITION_CHANGE and preview["has_excess"]:
        change_amount_override = _check_change_override(
            request, preview["change_amount"], change_currency, currency, change_rate
        )
        if preview["change_amount_kgs"] > Decimal(settings.CHANGE_CONFIRM_THRESHOLD_KGS):
            conv["reasons"].append(
                _("крупная сдача — %(a)s %(c)s")
                % {"a": preview["change_amount_kgs"], "c": settings.CURRENCY}
            )
            conv["requires_ack"] = True

    if conv["requires_ack"] and request.POST.get("risk_ack") != "1":
        messages.error(
            request,
            _("Требуется подтверждение платежа: %(reasons)s")
            % {"reasons": "; ".join(conv["reasons"])},
        )
        return redirect("pos:debt_pay_detail", pk=order.pk)

    try:
        record_payment(
            order,
            amount,
            user=request.user,
            method=method,
            currency=currency,
            rate_override=rate_override,
            excess_disposition=excess_disposition,
            change_currency=change_currency,
            change_amount_override=change_amount_override,
            change_adjust_reason=request.POST.get("change_adjust_reason", ""),
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("pos:debt_pay_detail", pk=order.pk)

    messages.success(request, _("Платёж записан."))
    return redirect("pos:sale_result", pk=order.pk)


@pos_view
@require_can_sell
def client_debt_pay(request, pk):
    """«Погасить долг по нескольким продажам» — one payment split oldest-
    first across a client's open same-currency sales (services.
    allocate_oldest_first / pay_oldest_first), one Payment per sale, никогда
    a second payment path. A plain GET-driven preview (?amount=…) — no
    HTMX needed for a screen this simple — shows exactly which sales would
    close before anything is confirmed."""
    client = get_object_or_404(Client, pk=pk)
    debts = client_debt(client)
    if not debts:
        return redirect("pos:client_detail", pk=client.pk)

    currency = request.GET.get("currency") or next(iter(debts))
    if currency not in debts:
        currency = next(iter(debts))

    amount = _parse_decimal(request.GET.get("amount"))
    allocation = allocate_oldest_first(client, currency, amount) if amount else None

    return render(
        request,
        "pos/client_debt_pay.html",
        {
            "client": client,
            "debts": debts,
            "currency": currency,
            "total_debt": debts.get(currency, Decimal("0")),
            "amount": amount,
            "allocation": allocation,
            "methods": Payment.METHOD_CHOICES,
            "active": "clients",
        },
    )


@pos_view
@require_can_sell
@require_POST
def client_debt_pay_confirm(request, pk):
    client = get_object_or_404(Client, pk=pk)
    currency = request.POST.get("currency") or settings.CURRENCY
    amount = _parse_decimal(request.POST.get("amount"))
    method = request.POST.get("method") or Payment.CASH
    base_url = reverse("pos:client_debt_pay", args=[client.pk])

    if not amount or amount <= 0:
        messages.error(request, _("Укажите сумму больше нуля."))
        return redirect(f"{base_url}?currency={currency}")

    try:
        payments = pay_oldest_first(client, currency, amount, user=request.user, method=method)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(f"{base_url}?currency={currency}&amount={amount}")

    messages.success(request, _("Записано платежей: %(n)s") % {"n": len(payments)})
    return redirect("pos:client_detail", pk=client.pk)


def _can_cancel(user, order) -> bool:
    if order.status != SaleOrder.CONFIRMED:
        return False
    if user.is_superuser:
        return True
    return (
        order.created_by_id == user.id
        and order.confirmed_at is not None
        # Compare in the shop's timezone, not UTC — otherwise "same-day" breaks
        # for the several evening hours when UTC has already rolled to the next
        # date but Bishkek hasn't (or vice versa in the early morning).
        and timezone.localtime(order.confirmed_at).date() == timezone.localdate()
    )


@pos_view
def sale_result(request, pk):
    if not request.user.has_perm("sales.view_saleorder"):
        raise PermissionDenied
    order = get_object_or_404(SaleOrder, pk=pk)
    if order.status == SaleOrder.DRAFT:
        return redirect("pos:sale_detail", pk=order.pk)
    items = list(order.items.select_related("variant__product__category"))
    status = order.payment_status if order.status == SaleOrder.CONFIRMED else None
    payments = list(order.payments.order_by("-created_at")) if order.client_id else []
    return render(
        request,
        "pos/result.html",
        {
            "order": order,
            "items": items,
            "status": status,
            "payments": payments,
            "can_cancel": _can_cancel(request.user, order),
            "active": "sale",
        },
    )


@pos_view
@require_can_sell
@require_POST
def sale_cancel(request, pk):
    order = get_object_or_404(SaleOrder, pk=pk)
    if not _can_cancel(request.user, order):
        raise PermissionDenied
    # [:255] matches StockMovement.reason's own max_length — truncated here,
    # server-side, so a crafted/overlong POST can never reach add_movement's
    # full_clean() and raise there instead, which the blanket `except
    # ValidationError: pass` below would then silently swallow as "already
    # cancelled" — a real cancel request failing with zero feedback.
    reason = request.POST.get("reason", "").strip()[:255]
    if not reason:
        messages.error(request, _("Укажите причину отмены."))
        return redirect("pos:sale_result", pk=order.pk)
    try:
        cancel_sale(order, user=request.user, reason=reason)
    except ValidationError:
        pass  # already cancelled — idempotent no-op
    return redirect("pos:sale_result", pk=order.pk)


@pos_view
@require_can_sell
def sale_return(request, pk):
    """Partial return/exchange. Same permission as cancel (Owner any; Editor own
    same-day). GET shows a per-line quantity form; POST applies it via
    services.return_items and comes back to the result screen."""
    order = get_object_or_404(SaleOrder, pk=pk)
    if not _can_cancel(request.user, order):
        raise PermissionDenied
    items = list(order.items.select_related("variant__product__category"))

    if request.method == "POST":
        returns = {}
        for item in items:
            try:
                qty = int(request.POST.get(f"return_{item.pk}", "0"))
            except (TypeError, ValueError):
                qty = 0
            if qty > 0:
                returns[item.pk] = qty
        reason = request.POST.get("reason", "").strip()[:255]  # matches StockMovement.reason
        if not reason:
            messages.error(request, _("Укажите причину возврата."))
            return redirect("pos:sale_return", pk=order.pk)
        try:
            return_items(order, returns, user=request.user, reason=reason)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("pos:sale_return", pk=order.pk)
        messages.success(request, _("Возврат оформлён."))
        return redirect("pos:sale_result", pk=order.pk)

    return render(request, "pos/return.html", {"order": order, "items": items, "active": "sale"})


# ---- Receipt PDF (read-only) + WhatsApp receipt/debt reminder (logged) -----


@rate_limit("receipt", 120, 300)
@pos_view
def receipt_download(request, pk):
    """Streams the receipt PDF, generated fresh from the database on THIS
    request — nothing is stored (a saved file would go stale the moment a
    return or repayment changes the numbers; the database never does) and
    nothing is written (a plain GET is correct precisely because this is a
    pure read — see the row-count test). Any authenticated staff who can view
    the sale can download its receipt: it carries no cost price or anything
    else a Viewer couldn't already see on this same sale's own page, so
    there's no reason to scope it to "own sale" the way share_receipt is.

    Access is LOGGED (IP + timestamp, via the logger only) for visibility —
    deliberately NOT into SecurityEvent/the DB: this view's whole point is
    being a pure read with zero writes (see the row-count test this
    docstring already points at), and a security audit row on every
    download would quietly break that guarantee for a feature the daily
    digest doesn't even need DB-level counts for.

    Rate limit 120 per IP per 5 minutes, not 30: the limit is keyed on IP and
    all staff share one office IP, so the ceiling is the whole shop's combined
    activity, not one person's. Re-printing a busy day's receipts in one go is
    ordinary end-of-day work and would have tripped a limit of 30 mid-task.
    120 still ends a scripted scrape of every sale long before it finishes."""
    if not request.user.has_perm("sales.view_saleorder"):
        raise PermissionDenied
    order = get_object_or_404(SaleOrder, pk=pk, status=SaleOrder.CONFIRMED)
    logger.info("Receipt accessed: order=%s ip=%s user=%s", pk, client_ip(request), request.user)
    # A filesystem path, not a <style> block, so this template never trips
    # the CSP inline-style test — see receipt.css's own @font-face rules for
    # why the receipt needs its Cyrillic glyphs embedded at all.
    css_path = Path(settings.BASE_DIR) / "static" / "pos" / "receipt.css"
    context = receipt_context(order)
    context["receipt_css_path"] = str(css_path)
    html = render_to_string("receipts/receipt.html", context)
    buffer = BytesIO()
    pisa.CreatePDF(html, dest=buffer, encoding="utf-8")
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    # Sortable + searchable in any file manager: sale number, then ISO date.
    # Latin "chek", not Cyrillic «чек» — a plain-ASCII filename needs no
    # RFC 5987 filename* encoding at all, sidestepping the real, historical
    # non-ASCII-filename quirks some Android/iOS in-app browsers and file
    # viewers (WhatsApp's own included) have had; nothing here could actually
    # be verified against a real device from this environment, so this is
    # the option with the least room for a device-specific surprise. The
    # order number is no longer hidden from the client (see receipt.html /
    # CLAUDE.md's "a receipt without a number isn't a receipt"), so putting
    # it in the filename too doesn't reopen anything that was deliberately hidden.
    date_str = (
        context["date"].strftime("%Y-%m-%d")
        if context["date"]
        else timezone.localdate().isoformat()
    )
    filename = f"ACOCOS-chek-{order.pk}-{date_str}.pdf"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@rate_limit("statement", 60, 300)
@pos_view
def statement_download(request, pk):
    """Streams the client statement PDF, generated fresh from the database
    on THIS request — same rules as receipt_download: nothing stored,
    nothing written, no signed link. ?from=/?to= narrow it to the same
    window shown on the client page; unset means full history.

    Rate limit 60 per IP per 5 minutes, not the receipt's 120 — a statement
    is a heavier, less frequent action (reviewing one client's whole
    history, not re-printing today's sales), so a lower ceiling still
    covers ordinary use while ending a scripted scrape of every client's
    statement well before it finishes. Keyed on IP like every other limit
    here, so the ceiling is the whole shop's shared activity, not one
    person's — see apps.core.ratelimit's own module docstring."""
    if not request.user.has_perm("sales.view_saleorder"):
        raise PermissionDenied
    client = get_object_or_404(Client, pk=pk)
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    logger.info("Statement accessed: client=%s ip=%s user=%s", pk, client_ip(request), request.user)
    css_path = Path(settings.BASE_DIR) / "static" / "pos" / "receipt.css"
    context = statement_context(client, date_from=date_from, date_to=date_to)
    context["receipt_css_path"] = str(css_path)
    html = render_to_string("receipts/statement.html", context)
    buffer = BytesIO()
    pisa.CreatePDF(html, dest=buffer, encoding="utf-8")
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    date_str = timezone.localdate().isoformat()
    filename = f"ACOCOS-vypiska-{client.pk}-{date_str}.pdf"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@pos_view
@require_POST
def share_receipt(request, pk):
    """Open WhatsApp with a short, link-free thank-you message, and log the
    touchpoint. wa.me can't attach a file — she downloads the PDF (see
    receipt_download / «Скачать чек») and attaches it herself inside
    WhatsApp; this button only opens the chat with the text ready. A no-op
    redirect back if the sale has no client/phone.

    POST-only and permission-gated because it WRITES an Interaction row. As a
    GET it was reachable by a Viewer (documented read-only) and, having no CSRF
    protection, could be fired by any page that got a logged-in manager to load
    an <img src="…/receipt/"> — silently forging touchpoint history. Scope is
    the SAME Editor(own, same-day)/Owner(any) rule as cancelling/returning a
    sale (_can_cancel) — CLAUDE.md's "Editor: own same-day; Owner: any" for
    this exact link."""
    if not request.user.has_perm("clients.add_interaction"):
        raise PermissionDenied
    order = get_object_or_404(SaleOrder, pk=pk, status=SaleOrder.CONFIRMED)
    if not _can_cancel(request.user, order):
        raise PermissionDenied
    if not order.client or not order.client.phone:
        return redirect("pos:sale_result", pk=order.pk)
    link = wa_link(order.client.phone, receipt_share_text(order))
    if not link:  # phone had no usable digits
        return redirect("pos:sale_result", pk=order.pk)
    Interaction.objects.create(
        client=order.client,
        kind=Interaction.MESSAGE,
        note=_("Чек отправлен"),
        created_by=request.user,
    )
    return redirect(link)


@pos_view
@require_POST
def debt_reminder(request, pk):
    """Open WhatsApp with a polite debt reminder for a client, and log it.
    POST-only and permission-gated for the same reason as share_receipt: it
    writes an Interaction, so it must not be reachable by a Viewer or by a
    cross-site GET."""
    if not request.user.has_perm("clients.add_interaction"):
        raise PermissionDenied
    client = get_object_or_404(Client, pk=pk)
    debts = client_debt(client)
    link = wa_link(client.phone, debt_reminder_text(client, debts)) if debts else ""
    if not link:
        return redirect("pos:client_detail", pk=client.pk)
    Interaction.objects.create(
        client=client,
        kind=Interaction.MESSAGE,
        note=_("Напоминание о долге"),
        created_by=request.user,
    )
    return redirect(link)


@pos_view
@require_POST
def send_statement(request, pk):
    """«Отправить выписку» — open WhatsApp with exactly the figure she
    already types by hand today: name + date + current balance, no item
    list, no URL (see apps.pos.messaging.statement_share_text). POST-only
    and permission-gated for the same reason as share_receipt/debt_reminder:
    it writes an Interaction, so it must not be reachable by a Viewer or by
    a cross-site GET. Unlike debt_reminder this fires even at zero balance —
    a statement is informational, not a nudge, so it isn't conditioned on
    `client_debt` being non-empty."""
    from apps.clients.services import client_debts_by_currency

    if not request.user.has_perm("clients.add_interaction"):
        raise PermissionDenied
    client = get_object_or_404(Client, pk=pk)
    closing = client_debts_by_currency(client=client).get(client.pk, {})
    text = statement_share_text(client, closing, timezone.localdate())
    link = wa_link(client.phone, text)
    if not link:  # phone had no usable digits
        return redirect("pos:client_detail", pk=client.pk)
    Interaction.objects.create(
        client=client,
        kind=Interaction.MESSAGE,
        note=_("Выписка отправлена"),
        created_by=request.user,
    )
    return redirect(link)


@pos_view
@require_POST
def toggle_do_not_remind(request, pk):
    """Flip «Не напоминать» — the Должники worklist's own per-client skip
    (see Client.do_not_remind's docstring: it hides ONLY the «Напомнить»
    action there, nothing else). Same write-permission gate as every other
    action on that list."""
    if not request.user.has_perm("clients.add_interaction"):
        raise PermissionDenied
    client = get_object_or_404(Client, pk=pk)
    client.do_not_remind = not client.do_not_remind
    client.save(update_fields=["do_not_remind"])
    return redirect("pos:debtors")


# ---- Today / Clients (read-only, Viewer allowed) ---------------------------


@pos_view
def today(request):
    summary = today_summary()
    today_date = timezone.localdate()
    # is_historical excluded — see SaleOrder.is_historical's own docstring:
    # a backdated entry must never count as today's activity, even in the
    # unlikely case she picks today's own date for one.
    orders = (
        SaleOrder.objects.filter(
            status=SaleOrder.CONFIRMED, confirmed_at__date=today_date, is_historical=False
        )
        .select_related("client")
        .order_by("-confirmed_at")
    )
    # Sum each order's own FROZEN total_kgs — never re-convert its currency
    # at today's live rate. A same-day rate refresh (the Курс «Обновить»
    # button) must not move an order confirmed earlier today; only
    # apps.reports.dashboard's approach (frozen totals, summed) agrees with
    # itself no matter when this page is loaded.
    revenue_base = orders.aggregate(s=Sum("total_kgs"))["s"] or Decimal("0")

    return render(
        request,
        "pos/today.html",
        {
            "summary": summary,
            "orders": orders,
            "revenue_base": revenue_base,
            "base_currency": settings.CURRENCY,
            "active": "today",
        },
    )


def _debt_label(pos: dict) -> str:
    """'7 400 сом · 50 $' from {currency: amount} (positive debts only) — same
    money formatting rule as everywhere else (POS-DESIGN.md)."""
    from apps.pos.templatetags.pos_extras import money_filter

    return " · ".join(money_filter(amt, cur) for cur, amt in pos.items())


@pos_view
def clients(request):
    from decimal import Decimal

    from apps.clients.services import client_debts_by_currency

    q = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "name")
    flt = request.GET.get("filter", "all")

    qs = Client.objects.filter(is_active=True)
    if q:
        qs = qs.filter(
            Q(phone__icontains=q) | Q(first_name__icontains=q) | Q(descriptor__icontains=q)
        )
    if flt == "consent":
        qs = qs.filter(marketing_consent=True)

    debts = client_debts_by_currency()
    people = list(qs)
    for c in people:
        pos = {cur: amt for cur, amt in debts.get(c.pk, {}).items() if amt > 0}
        c.debt_sum = sum(pos.values(), Decimal("0"))
        c.debt_label = _debt_label(pos)
    if flt == "debt":
        people = [c for c in people if c.debt_sum > 0]

    if sort == "debt":
        people.sort(key=lambda c: c.debt_sum, reverse=True)
    elif sort == "recent":
        people.sort(key=lambda c: c.created_at, reverse=True)
    else:
        sort = "name"
        people.sort(key=lambda c: (c.first_name.lower(), c.descriptor.lower()))

    opening_balance_progress = None
    if request.user.is_superuser:
        # «Старый долг внесён для N из M клиентов» — Owner-only migration
        # progress readout (see apps.pos.views.opening_balance_add), so she
        # can tell how much of the by-hand entry is left without opening
        # every client individually. Computed only for Owner: a Django
        # request-count query staff never see doesn't need to run for them.
        from apps.clients.models import ClientOpeningBalance

        total_clients = Client.objects.filter(is_active=True).count()
        covered_clients = ClientOpeningBalance.objects.values("client_id").distinct().count()
        opening_balance_progress = {"n": covered_clients, "m": total_clients}

    return render(
        request,
        "pos/clients.html",
        {
            "clients": people,
            "q": q,
            "sort": sort,
            "filter": flt,
            "total": len(people),
            "opening_balance_progress": opening_balance_progress,
            "active": "clients",
        },
    )


@pos_view
def debtors(request):
    """«Должники» — every client with a balance, highest debt first, so she
    can work through them in one pass instead of opening clients one by one.
    Reuses apps.clients.services.debtors_report_rows() directly (the same
    aggregation the daily report's Долги sheet and the /debts bot command
    already use) rather than re-deriving debt here — this list can never
    disagree with client_debts_by_currency. Read-only (Viewer may open it);
    the «Выписка»/«Напомнить» actions on each row carry their own write
    gates (send_statement/debt_reminder, both clients.add_interaction)."""
    from apps.clients.services import debtors_report_rows

    rows = debtors_report_rows()
    return render(request, "pos/debtors.html", {"rows": rows, "active": "clients"})


def _order_item_summary(order) -> str:
    """ "Evening Dress / M / red и ещё 2" — the first line's own variant
    description (see ProductVariant.__str__) plus a count of the rest, for a
    compact one-line "what was bought" on the client page's debt/history
    rows. Requires order.items (with variant__product) to already be
    prefetched by the caller — never re-queries per row."""
    items = list(order.items.all())
    if not items:
        return ""
    first = str(items[0].variant)
    if len(items) > 1:
        return _("%(first)s и ещё %(n)d") % {"first": first, "n": len(items) - 1}
    return first


@pos_view
def client_detail(request, pk):
    from apps.clients.services import client_debt_and_credit, client_statement

    client = get_object_or_404(Client, pk=pk)
    # ONE indexed query pair (client_debt()+client_credits() back to back
    # would each independently re-aggregate) — this is the highest-traffic
    # client-facing page in the app, opened before/after nearly every sale.
    debts, credits = client_debt_and_credit(client)
    orders = (
        client.sales.filter(status=SaleOrder.CONFIRMED)
        .prefetch_related("payments", "items__variant__product__category")  # balance + item summary
        .order_by("-confirmed_at")[:20]
    )
    # Each sale carries its own outstanding balance, so the debt/history
    # blocks show WHICH purchases they came from instead of only a single
    # total at the top. Derived per order (never stored) — same numbers the
    # POS and the debts report use, so the three can't drift apart. Split
    # into unpaid (the debt block) vs. fully-paid (История покупок) here
    # rather than in the template, so both stay simple {% for %} loops.
    order_rows = [
        {
            "order": o,
            "paid": o.paid_amount,
            "balance": o.balance,
            "item_summary": _order_item_summary(o),
        }
        for o in orders
    ]
    unpaid_rows = [row for row in order_rows if row["balance"] > 0]
    paid_rows = [row for row in order_rows if row["balance"] <= 0]
    interactions = client.interactions.order_by("-created_at")[:20]
    # «Выписка» — the chronological running-balance ledger (CLAUDE.md-style
    # per-sale debt is still what confirm_sale/record_payment enforce; this
    # is a read-only VIEW over that same data — see client_statement's own
    # docstring). ?from=/?to= narrow it to match a specific paper statement
    # she's reconciling against; unset shows full history.
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    statement = client_statement(client, date_from=date_from, date_to=date_to)
    return render(
        request,
        "pos/client_detail.html",
        {
            "client": client,
            "debts": debts,
            "credits": credits,
            "unpaid_rows": unpaid_rows,
            "paid_rows": paid_rows,
            "interactions": interactions,
            "statement": statement,
            "statement_from": date_from,
            "statement_to": date_to,
            "active": "clients",
        },
    )


@pos_view
def opening_balance_add(request, pk):
    """«Добавить старый долг» — manual, one-at-a-time entry for pre-system
    debt. Replaces a bulk xlsx importer that shipped and was then removed:
    a one-time script was judged more risk than it saved for the handful of
    clients she actually needs to backfill. Owner-only, enforced here (never
    just hidden in the UI) — the same tier as ClientOpeningBalanceAdmin and
    rate_edit/rate_save.

    Upserts by (client, currency, as_of_date) — ClientOpeningBalance's own
    UniqueConstraint — so re-entering the same date for the same currency
    corrects the earlier figure in place (she WILL make typos) rather than
    erroring on a duplicate. created_by is only set on first creation, never
    overwritten by a later correction."""
    if not request.user.is_superuser:
        raise PermissionDenied
    from apps.clients.models import ClientOpeningBalance

    client = get_object_or_404(Client, pk=pk)
    error = None
    if request.method == "POST":
        amount = _parse_signed_decimal(request.POST.get("amount"))
        currency = request.POST.get("currency") or settings.CURRENCY
        as_of_date = _parse_date(request.POST.get("as_of_date"))
        note = (request.POST.get("note") or "").strip()
        if amount is None or amount == 0:
            error = _("Укажите сумму (ненулевую).")
        elif currency not in CURRENCY_CODES:
            error = _("Неизвестная валюта.")
        elif as_of_date is None:
            error = _("Укажите дату.")
        else:
            existing = ClientOpeningBalance.objects.filter(
                client=client, currency=currency, as_of_date=as_of_date
            ).first()
            if existing:
                existing.amount = amount
                existing.note = note
                existing.save(update_fields=["amount", "note"])
            else:
                ClientOpeningBalance.objects.create(
                    client=client,
                    currency=currency,
                    as_of_date=as_of_date,
                    amount=amount,
                    note=note,
                    created_by=request.user,
                )
            return redirect("pos:client_detail", pk=client.pk)

    return render(
        request,
        "pos/opening_balance_add.html",
        {
            "client": client,
            "error": error,
            "today": timezone.localdate(),
            "currency_codes": CURRENCY_CODES,
            "posted": request.POST if request.method == "POST" else None,
            "active": "clients",
        },
    )
