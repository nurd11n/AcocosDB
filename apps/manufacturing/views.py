"""Manufacturing /manufacturing/ surface — Owner-only throughout (same tier
as cost price and profit), same gate pattern as apps.pos.views.
opening_balance_add: @pos_view for the login+OTP check every /pos/-adjacent
view already needs, plus an explicit is_superuser check here (never just
hidden behind BUSINESS_MODEL_PERMISSIONS not listing these models — that
blocks Editor/Viewer already, but a view-level check is the same defence-
in-depth every other money-affecting control in this codebase applies)."""

from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.currency import format_money, rate_info
from apps.core.ratelimit import rate_limit
from apps.pos.decorators import pos_view

from .models import Contractor, ContractorTransaction, Expense, ProductionRun
from .services import (
    contractor_balance,
    contractor_statement,
    record_expense,
    record_production_run,
)


# ProductVariant/ProductionRun quantities are PositiveIntegerField (int32,
# max 2 147 483 647). A crafted POST above that raises a raw psycopg
# DataError on Postgres — an uncaught 500, not a Russian error message. This
# cap sits far below the DB ceiling AND far above any real batch this shop
# will ever cut, so it can only ever be hit by a typo or a crafted request.
MAX_BATCH_QTY = 100_000


def _require_owner(request):
    if not request.user.is_superuser:
        raise PermissionDenied


def _parse_decimal(raw) -> Decimal | None:
    try:
        value = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return value if value >= 0 else None


def _parse_date(raw):
    if not raw:
        return None
    try:
        return date_cls.fromisoformat(raw)
    except ValueError:
        return None


@pos_view
def contractors(request):
    _require_owner(request)
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, _("Укажите имя подрядчика."))
        else:
            Contractor.objects.create(
                name=name,
                contact=(request.POST.get("contact") or "").strip(),
                note=(request.POST.get("note") or "").strip(),
            )
            messages.success(request, _("Подрядчик добавлен."))
        return redirect("manufacturing:contractors")

    balances = contractor_balance()
    rows = []
    for c in Contractor.objects.filter(is_active=True).order_by("name"):
        rows.append({"contractor": c, "balances": balances.get(c.pk, {})})
    return render(
        request, "manufacturing/contractors.html", {"rows": rows, "active": "manufacturing"}
    )


@pos_view
def contractor_detail(request, pk):
    _require_owner(request)
    contractor = get_object_or_404(Contractor, pk=pk)
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    statement = contractor_statement(contractor, date_from, date_to)
    runs = ProductionRun.objects.filter(contractor=contractor).select_related(
        "variant__product__category"
    )[:30]
    return render(
        request,
        "manufacturing/contractor_detail.html",
        {
            "contractor": contractor,
            "balances": contractor_balance(contractor).get(contractor.pk, {}),
            "statement": statement,
            "statement_from": date_from,
            "statement_to": date_to,
            "runs": runs,
            "active": "manufacturing",
        },
    )


@pos_view
def contractor_transaction_add(request, pk):
    _require_owner(request)
    contractor = get_object_or_404(Contractor, pk=pk)
    if request.method == "POST":
        amount = _parse_decimal(request.POST.get("amount"))
        kind = request.POST.get("kind")
        currency = request.POST.get("currency") or "KGS"
        if not amount or amount <= 0:
            messages.error(request, _("Укажите сумму больше нуля."))
        elif kind not in (ContractorTransaction.ACCRUAL, ContractorTransaction.PAYMENT):
            messages.error(request, _("Некорректный тип операции."))
        else:
            from apps.core.currency import snapshot_rate_to_base

            entry_date = _parse_date(request.POST.get("date")) or timezone.localdate()
            rate = snapshot_rate_to_base(currency, entry_date)
            ContractorTransaction.objects.create(
                contractor=contractor,
                kind=kind,
                amount=amount,
                currency=currency,
                rate_to_kgs=rate,
                date=entry_date,
                note=(request.POST.get("note") or "").strip(),
                created_by=request.user,
            )
            messages.success(request, _("Операция добавлена."))
            return redirect("manufacturing:contractor_detail", pk=contractor.pk)
    return render(
        request,
        "manufacturing/contractor_transaction_add.html",
        {"contractor": contractor, "today": timezone.localdate(), "active": "manufacturing"},
    )


def _build_production_grid(product, order_item=None, prefill=None):
    """Every active variant of `product`, arranged into a size x color
    matrix for the batch form — sizes as rows (in the order
    ProductVariant.Meta.ordering already sorted them: size, then color),
    colors as columns. A (size, color) pair with no real variant renders as
    an empty cell — not every size necessarily comes in every color.

    `prefill` ({variant_id: {"accepted": int, "defect": int}}) re-populates
    quantities after a failed submit re-renders this same grid, so nothing
    typed has to be retyped. Order-item pre-selection (the ?order_item=N
    entry point) suggests remaining_to_produce the same way the old
    single-variant form did, UNLESS a real prefill value already came back
    from a failed submit — that always wins, it's what the user just typed.
    Both are attached directly onto each variant object (matching how
    apps.pos.views._group_variants_for_picker attaches .available_qty) so
    the template needs no custom dict-lookup-by-variable-key filter."""
    variants = list(product.variants.filter(is_active=True))
    prefill = prefill or {}
    for v in variants:
        entry = prefill.get(v.pk)
        if entry is not None:
            v.prefill_accepted = entry["accepted"]
            v.prefill_defect = entry["defect"]
        elif order_item is not None and order_item.variant_id == v.pk:
            v.prefill_accepted = order_item.remaining_to_produce
            v.prefill_defect = 0
        else:
            v.prefill_accepted = None
            v.prefill_defect = None
        v.is_order_item_variant = bool(order_item is not None and order_item.variant_id == v.pk)

    sizes, colors = [], []
    by_cell = {}
    for v in variants:
        if v.size not in sizes:
            sizes.append(v.size)
        if v.color not in colors:
            colors.append(v.color)
        by_cell[(v.size, v.color)] = v
    rows = [
        {
            "size": size,
            "label": size or "—",
            "cells": [by_cell.get((size, color)) for color in colors],
        }
        for size in sizes
    ]
    return {
        "variants": variants,
        "size_labels": [s or "—" for s in sizes],
        "color_labels": [c or "—" for c in colors],
        "rows": rows,
    }


def _order_item_from_request(request):
    from apps.orders.models import OrderItem

    order_item_id = request.GET.get("order_item") or request.POST.get("order_item")
    if not order_item_id:
        return None
    return OrderItem.objects.filter(pk=order_item_id).select_related("variant__product").first()


@pos_view
def production_add(request):
    """The batch production entry point — select a product once, then
    record accepted/defect quantities for as many of its size/color
    variants as actually came in, in one submit. Keeps the data model and
    record_production_run completely unchanged (stock stays per-variant,
    which reservations/the production queue/COGS all depend on) — this is
    purely a faster way to call it once per variant instead of once per
    page load."""
    _require_owner(request)
    order_item = _order_item_from_request(request)

    if request.method == "POST":
        return _production_batch_post(request, order_item)

    product = order_item.variant.product if order_item else None
    return render(
        request,
        "manufacturing/production_add.html",
        {
            "order_item": order_item,
            "product": product,
            "grid": _build_production_grid(product, order_item=order_item) if product else None,
            "contractors": Contractor.objects.filter(is_active=True).order_by("name"),
            "active": "manufacturing",
        },
    )


@rate_limit("search", 300, 60)
@pos_view
def production_search(request):
    """HTMX product search — "select product once (searchable, not a flat
    variant list)": replaces the old flat <select> of every active variant
    in the whole catalog with the same search-then-pick shape /pos/'s own
    product grid uses, one layer up (a product, not yet a variant)."""
    _require_owner(request)
    from apps.inventory.models import Product

    q = request.GET.get("q", "").strip()
    products = Product.objects.filter(is_active=True).select_related("category")
    if q:
        products = products.filter(Q(name__icontains=q) | Q(variants__sku__icontains=q)).distinct()
    products = products.order_by("name")[:30]
    return render(
        request, "manufacturing/partials/production_search_results.html", {"products": products}
    )


@pos_view
def production_grid_view(request, product_id):
    """HTMX endpoint: the size x color grid + shared batch fields (Подрядчик/
    Валюта/Общая стоимость/Заметка) for one product — everything the batch
    form needs below the product search, swapped in as one chunk once a
    product is picked. Shared with production_add's own initial render (the
    ?order_item=N case renders this same partial directly, no round trip
    needed since the product is already known)."""
    _require_owner(request)
    from apps.inventory.models import Product

    product = get_object_or_404(Product, pk=product_id, is_active=True)
    order_item = _order_item_from_request(request)
    matched_order_item = (
        order_item if order_item and order_item.variant.product_id == product.pk else None
    )
    return render(
        request,
        "manufacturing/partials/production_grid.html",
        {
            "product": product,
            "grid": _build_production_grid(product, order_item=matched_order_item),
            "order_item": matched_order_item,
            "contractors": Contractor.objects.filter(is_active=True).order_by("name"),
        },
    )


def _parse_batch_rows(post) -> tuple[list[dict], bool]:
    """The grid's accepted_<id>/defect_<id> field pairs -> one dict per cell
    that actually has a quantity, plus a flag saying whether anything was
    malformed.

    A cell left at 0/0 (or empty, which is what an untouched input posts) is
    SKIPPED, never returned — that is what "a 0-qty cell writes nothing"
    means: it never reaches record_production_run at all, rather than being
    recorded as an empty run.

    Anything malformed sets the flag instead of raising, so the caller can
    reject the WHOLE batch with one Russian message rather than saving the
    valid rows and silently dropping the rest. Quantities are capped at
    MAX_BATCH_QTY here, before the DB gets a chance to raise a raw
    integer-out-of-range DataError."""
    rows: list[dict] = []
    bad_input = False
    for key, raw_value in post.items():
        if not key.startswith("accepted_"):
            continue
        variant_id_raw = key[len("accepted_") :]
        try:
            variant_id = int(variant_id_raw)
            accepted = int(raw_value or 0)
            defect = int(post.get(f"defect_{variant_id_raw}") or 0)
        except ValueError:
            bad_input = True
            continue
        if accepted < 0 or defect < 0 or accepted > MAX_BATCH_QTY or defect > MAX_BATCH_QTY:
            bad_input = True
            continue
        if accepted == 0 and defect == 0:
            continue
        rows.append({"variant_id": variant_id, "accepted": accepted, "defect": defect})
    return rows, bad_input


def _production_batch_post(request, order_item):
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from apps.inventory.models import Product, ProductVariant
    from apps.orders.services import mark_produced
    from .services import split_cost_proportionally

    # get_object_or_404 only converts DoesNotExist to a 404 — an empty or
    # non-numeric "contractor" (an empty <select>, or a crafted POST) would
    # otherwise raise a bare ValueError and 500. Missing/invalid resolves to
    # None here and joins the same graceful error cascade below instead.
    try:
        contractor = Contractor.objects.get(pk=int(request.POST.get("contractor") or ""))
    except (TypeError, ValueError, Contractor.DoesNotExist):
        contractor = None
    currency = request.POST.get("currency") or "KGS"
    note = (request.POST.get("note") or "").strip()
    # A blank cost is legitimate (production recorded without cost data yet).
    # A NEGATIVE or unparseable one is not, and must not quietly become 0 —
    # that would file the batch with no cost at all, leaving cost_price stale
    # at whatever the last run set while the user believes they just entered
    # this batch's cost.
    raw_total_cost = (request.POST.get("total_cost") or "").strip()
    total_cost = _parse_decimal(raw_total_cost) if raw_total_cost else Decimal("0")
    bad_cost = total_cost is None
    if bad_cost:
        total_cost = Decimal("0")

    rows, bad_input = _parse_batch_rows(request.POST)

    product = None
    variants_by_id = {}
    if rows and not bad_input:
        variant_ids = [r["variant_id"] for r in rows]
        variants_by_id = {
            v.pk: v
            for v in ProductVariant.objects.filter(
                pk__in=variant_ids, is_active=True
            ).select_related("product")
        }
        product_ids = {v.product_id for v in variants_by_id.values()}
        if len(variants_by_id) == len(set(variant_ids)) and len(product_ids) == 1:
            product = Product.objects.get(pk=next(iter(product_ids)))

    # Same rule apps.manufacturing.views.expenses and record_payment already
    # enforce: a foreign-currency amount with NO rate on record must save
    # NOTHING. snapshot_rate_to_base falls back to 1.0 by design (a SALE must
    # never fail for lack of a rate) — but here that silently files 1 000 $ of
    # fabric as 1 000 сом, understating it ~87x straight into cost_price and
    # therefore into COGS and profit, invisibly. Only matters when there IS a
    # cost to convert; a costless run in any currency is fine.
    missing_rate = total_cost > 0 and currency != settings.CURRENCY and rate_info(currency) is None

    error = None
    if contractor is None:
        error = _("Выберите подрядчика.")
    elif bad_cost:
        error = _("Некорректная стоимость партии.")
    elif bad_input:
        error = _("Некорректное количество — проверьте принято/брак.")
    elif not rows:
        error = _("Укажите количество хотя бы для одной позиции.")
    elif product is None:
        error = _("Один или несколько товаров не найдены — попробуйте выбрать товар заново.")
    elif missing_rate:
        error = _("Нет курса для %(c)s — производство не записано. Обновите курс и повторите.") % {
            "c": currency
        }
    else:
        weights = [r["accepted"] for r in rows]
        shares = split_cost_proportionally(total_cost, weights)
        try:
            with transaction.atomic():
                for row, share in zip(rows, shares):
                    variant = variants_by_id[row["variant_id"]]
                    if order_item is not None and variant.pk == order_item.variant_id:
                        # THIS row is the order's own line — route through
                        # mark_produced (which calls record_production_run
                        # itself, then does the order-specific bookkeeping:
                        # produced_qty, status advance) so «Запустить
                        # производство» keeps working exactly as before, even
                        # though it's now one row inside a larger batch.
                        mark_produced(
                            order_item,
                            row["accepted"],
                            user=request.user,
                            defect_qty=row["defect"],
                            contractor=contractor,
                            material_cost=share,
                            currency=currency,
                            note=note,
                        )
                    else:
                        record_production_run(
                            variant=variant,
                            contractor=contractor,
                            accepted_qty=row["accepted"],
                            defect_qty=row["defect"],
                            material_cost=share,
                            currency=currency,
                            note=note,
                            user=request.user,
                        )
        except ValidationError as exc:
            error = "; ".join(exc.messages)
        except ValueError as exc:
            error = str(exc)
        else:
            messages.success(request, _("Производство записано: %(n)s позиций.") % {"n": len(rows)})
            if order_item is not None:
                return redirect("orders:detail", pk=order_item.order_id)
            return redirect("manufacturing:contractors")

    # Any failure re-renders the SAME grid rather than redirecting — a
    # redirect would silently drop everything just typed, exactly the kind
    # of lost-work moment CLAUDE.md's draft-persistence rules exist to avoid
    # elsewhere in this app. Quantities are read back from what was just
    # posted so nothing has to be retyped.
    messages.error(request, error)
    prefill = {r["variant_id"]: r for r in rows} if not bad_input else {}
    if product is None and order_item is not None:
        product = order_item.variant.product
    return render(
        request,
        "manufacturing/production_add.html",
        {
            "order_item": order_item,
            "product": product,
            "grid": (
                _build_production_grid(product, order_item=order_item, prefill=prefill)
                if product
                else None
            ),
            "contractors": Contractor.objects.filter(is_active=True).order_by("name"),
            "active": "manufacturing",
            "prefill": prefill,
            "contractor_id": request.POST.get("contractor"),
            "currency_value": currency,
            "total_cost_value": request.POST.get("total_cost", ""),
            "note_value": note,
        },
    )


@pos_view
def expenses(request):
    _require_owner(request)
    if request.method == "POST":
        amount = _parse_decimal(request.POST.get("amount"))
        category = request.POST.get("category")
        currency = request.POST.get("currency") or "KGS"
        entry_date = _parse_date(request.POST.get("date")) or timezone.localdate()
        # Same rule payments already enforce (apps.sales.services.record_payment):
        # a foreign-currency amount with NO rate on record must save NOTHING,
        # never fall back to snapshot_rate_to_base's silent 1.0 — that would
        # file 10 $ as 10 сом, understating the expense ~87x with no warning.
        missing_rate = currency != settings.CURRENCY and rate_info(currency) is None
        if not amount or amount <= 0:
            messages.error(request, _("Укажите сумму больше нуля."))
        elif category not in dict(Expense.CATEGORY_CHOICES):
            messages.error(request, _("Некорректная категория."))
        elif missing_rate:
            messages.error(
                request,
                _("Нет курса для %(c)s — расход не сохранён. Обновите курс и повторите.")
                % {"c": currency},
            )
        else:
            contractor_id = request.POST.get("contractor") or None
            contractor = (
                Contractor.objects.filter(pk=contractor_id).first() if contractor_id else None
            )
            expense = record_expense(
                date=entry_date,
                category=category,
                amount=amount,
                currency=currency,
                contractor=contractor,
                note=(request.POST.get("note") or "").strip(),
                user=request.user,
            )
            # Confirm what was ACTUALLY filed, in both the typed currency and
            # сом. A currency picked by mistake is otherwise invisible until it
            # has already inflated every total on the page and the dashboard.
            if expense.currency != settings.CURRENCY:
                messages.success(
                    request,
                    _("Расход добавлен: %(amt)s ≈ %(kgs)s")
                    % {
                        "amt": format_money(expense.amount, expense.currency),
                        "kgs": format_money(expense.amount_kgs, settings.CURRENCY),
                    },
                )
            else:
                messages.success(
                    request,
                    _("Расход добавлен: %(amt)s")
                    % {"amt": format_money(expense.amount, expense.currency)},
                )
            return redirect("manufacturing:expenses")

    from datetime import date as date_cls

    from .dashboard import expenses_by_section

    rows = Expense.objects.select_related("contractor", "variant").all()[:100]
    return render(
        request,
        "manufacturing/expenses.html",
        {
            "rows": rows,
            "categories": Expense.CATEGORY_CHOICES,
            "contractors": Contractor.objects.filter(is_active=True).order_by("name"),
            # All-time — this page has no period filter of its own (unlike
            # the dashboard); classifies EVERY recorded expense, not just
            # the 100 shown below.
            "sections": expenses_by_section(date_cls(2000, 1, 1), timezone.localdate()),
            "today": timezone.localdate(),
            # So the list can show «≈ N сом» beside a non-KGS row (CLAUDE.md's
            # money rule) — the page mixes currencies in one table whose
            # totals are all сом-converted.
            "base_currency": settings.CURRENCY,
            "active": "manufacturing",
        },
    )


@pos_view
def manufacturing_dashboard(request):
    """Расходы by category/period, production volume, defect rate per
    contractor (ranked), real vs assumed cost per variant, contractor
    balances, work-in-progress, net profit after overhead — Owner-only,
    same tier as apps.reports.views.dashboard. `period` reuses the SAME
    resolver/window logic the main dashboard uses (apps.reports.dashboard.
    resolve_period/_windows) so "last 30 days" means the same thing on both
    pages."""
    _require_owner(request)
    from apps.reports.dashboard import dashboard_data

    from .dashboard import manufacturing_dashboard_data

    period = request.GET.get("period", "")
    main = dashboard_data(period)  # public API — same period resolution as /dashboard/
    data = manufacturing_dashboard_data(
        main["cal_start"], main["cal_end"], main["metrics"]["profit"]["value"]
    )
    return render(
        request,
        "manufacturing/dashboard.html",
        {
            **data,
            "period": main["period"],
            "period_label": main["period_label"],
            "periods": main["periods"],
            "active": "manufacturing",
        },
    )
