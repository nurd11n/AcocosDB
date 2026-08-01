"""Manager-facing sales terminal at /pos/. Server-rendered + HTMX partial
swaps — no API layer, no JS framework. Every screen calls the existing
apps/*/services.py functions; nothing here recomputes stock, money, or debt.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

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
from apps.inventory.cache import catalog_version
from apps.inventory.models import Product, ProductVariant, StockMovement
from apps.sales.models import Payment, SaleItem, SaleOrder
from apps.sales.services import (
    cancel_sale,
    compute_change_preview,
    confirm_sale,
    record_payment,
    return_items,
    today_summary,
)

from .decorators import pos_view
from .messaging import debt_reminder_text, receipt_text, wa_link

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
    items = list(order.items.select_related("variant__product"))
    total = sum((i.line_total for i in items), Decimal("0"))
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
        "total": total,
        "total_conv": total_conv,
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
    """On-demand NBKR pull — the view + fetch logic stay live and reachable at
    this URL, but nbkr.kg blocks the production server's IP outright (it works
    fine from a local dev machine, confirmed separately), so no /pos/ button
    currently calls this — see the removed-button note in sale_detail.html and
    rate_modal.html. Owner hand-enters rates instead (rate_edit/rate_save).
    Fetches the latest rates (owner manual overrides still win), then re-renders
    the rate strip. On a network/parse failure it just keeps the last known
    rates — same fail-soft contract as the daily fetch, never a 500. Allowed
    for Editor/Manager AND Owner (require_can_sell) — it only pulls the
    official number, unlike a manual override which is Owner-only."""
    from xml.etree import ElementTree

    import requests

    from apps.core.management.commands.fetch_rates import fetch_nbkr_rates

    try:
        fetch_nbkr_rates(changed_by=request.user)
    except (requests.RequestException, ElementTree.ParseError):
        pass  # keep last known rates
    return render(request, "pos/partials/rates.html", {"rates": _today_rates()})


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
    route them to Сегодня instead of a dead-end 403."""
    if not request.user.has_perm("sales.add_saleorder"):
        return redirect("pos:today")
    order = None
    if request.GET.get("new") != "1":
        order = (
            SaleOrder.objects.filter(created_by=request.user, status=SaleOrder.DRAFT)
            .order_by("-created_at")
            .first()
        )
    if order is None:
        order = SaleOrder.objects.create(created_by=request.user, channel=SaleOrder.SHOP)
    return redirect("pos:sale_detail", pk=order.pk)


@pos_view
@require_can_sell
def sale_detail(request, pk):
    order = get_object_or_404(SaleOrder, pk=pk, created_by=request.user)
    if order.status != SaleOrder.DRAFT:
        return redirect("pos:sale_result", pk=order.pk)
    context = {
        **_sale_body_context(order, can_override_rate=request.user.is_superuser),
        "active": "sale",
        "rates": _today_rates(),
    }
    return render(request, "pos/sale_detail.html", context)


# ---- Client section (HTMX partials) ---------------------------------------


@pos_view
@require_can_sell
def client_search(request, pk):
    order = _own_draft_or_404(request, pk)
    q = request.GET.get("q", "").strip()
    if q:
        clients = Client.objects.filter(
            Q(phone__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
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
    order.client = get_object_or_404(Client, pk=client_id)
    order.save(update_fields=["client"])
    return render(request, "pos/partials/client_section.html", {"order": order})


@pos_view
@require_can_sell
@require_POST
def client_clear(request, pk):
    order = _own_draft_or_404(request, pk)
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
    last_name = request.POST.get("last_name", "").strip()
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
                    "last_name": last_name,
                    "phone": phone,
                },
            },
        )

    client = Client.objects.create(
        first_name=first_name, last_name=last_name, phone=phone, source=Client.SHOP
    )
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
    products = Product.objects.filter(is_active=True).select_related("category")
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
    product = get_object_or_404(Product, pk=product_id)
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
    variant = get_object_or_404(ProductVariant, pk=request.POST.get("variant_id"))
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
            _sale_body_context(order, error=error, can_override_rate=request.user.is_superuser),
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
        _sale_body_context(order, error=error, can_override_rate=request.user.is_superuser),
    )


@pos_view
@require_can_sell
@require_POST
def item_remove(request, pk, item_id):
    order = _own_draft_or_404(request, pk)
    order.items.filter(pk=item_id).delete()
    return render(
        request,
        "pos/partials/sale_body.html",
        _sale_body_context(order, can_override_rate=request.user.is_superuser),
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
        confirm_sale(order, user=request.user)
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
    return redirect("pos:sale_result", pk=order.pk)


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
    items = list(order.items.select_related("variant__product"))
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
    try:
        cancel_sale(order, user=request.user)
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
    items = list(order.items.select_related("variant__product"))

    if request.method == "POST":
        returns = {}
        for item in items:
            try:
                qty = int(request.POST.get(f"return_{item.pk}", "0"))
            except (TypeError, ValueError):
                qty = 0
            if qty > 0:
                returns[item.pk] = qty
        try:
            return_items(order, returns, user=request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("pos:sale_return", pk=order.pk)
        messages.success(request, _("Возврат оформлён."))
        return redirect("pos:sale_result", pk=order.pk)

    return render(request, "pos/return.html", {"order": order, "items": items, "active": "sale"})


# ---- WhatsApp receipt + debt reminder (tap-to-send, logged) ----------------


@pos_view
@require_POST
def share_receipt(request, pk):
    """Open WhatsApp with a ready-to-send receipt for a confirmed sale, and log
    the touchpoint. A no-op redirect back if the sale has no client/phone.

    POST-only and permission-gated because it WRITES an Interaction row. As a
    GET it was reachable by a Viewer (documented read-only) and, having no CSRF
    protection, could be fired by any page that got a logged-in manager to load
    an <img src="…/receipt/"> — silently forging touchpoint history."""
    if not request.user.has_perm("clients.add_interaction"):
        raise PermissionDenied
    order = get_object_or_404(SaleOrder, pk=pk, status=SaleOrder.CONFIRMED)
    if not order.client or not order.client.phone:
        return redirect("pos:sale_result", pk=order.pk)
    link = wa_link(order.client.phone, receipt_text(order))
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


# ---- Today / Clients (read-only, Viewer allowed) ---------------------------


@pos_view
def today(request):
    summary = today_summary()
    today_date = timezone.localdate()
    orders = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date=today_date)
        .select_related("client")
        .order_by("-confirmed_at")
    )
    unreviewed_count = Payment.objects.filter(created_at__date=today_date, reviewed=False).count()

    revenue_base = Decimal("0")
    skipped_currencies = []
    for cur, amt in summary["revenue_by_currency"].items():
        conv = to_base(amt, cur, today_date)
        if conv is None:
            skipped_currencies.append(cur)
        else:
            revenue_base += conv

    return render(
        request,
        "pos/today.html",
        {
            "summary": summary,
            "orders": orders,
            "unreviewed_count": unreviewed_count,
            "revenue_base": revenue_base,
            "base_currency": settings.CURRENCY,
            "skipped_currencies": skipped_currencies,
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
            Q(phone__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
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
        people.sort(key=lambda c: (c.first_name.lower(), c.last_name.lower()))

    return render(
        request,
        "pos/clients.html",
        {
            "clients": people,
            "q": q,
            "sort": sort,
            "filter": flt,
            "total": len(people),
            "active": "clients",
        },
    )


@pos_view
def client_detail(request, pk):
    from apps.clients.services import client_credits

    client = get_object_or_404(Client, pk=pk)
    debts = client_debt(client)
    credits = client_credits(client)
    orders = client.sales.filter(status=SaleOrder.CONFIRMED).order_by("-confirmed_at")[:20]
    interactions = client.interactions.order_by("-created_at")[:20]
    payments = client.payments.order_by("-created_at")[:20]
    return render(
        request,
        "pos/client_detail.html",
        {
            "client": client,
            "debts": debts,
            "credits": credits,
            "orders": orders,
            "payments": payments,
            "interactions": interactions,
            "active": "clients",
        },
    )
