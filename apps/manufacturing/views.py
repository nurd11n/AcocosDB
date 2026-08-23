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
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.currency import format_money, rate_info
from apps.pos.decorators import pos_view

from .models import Contractor, ContractorTransaction, Expense, ProductionRun
from .services import (
    contractor_balance,
    contractor_statement,
    record_expense,
    record_production_run,
)


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


@pos_view
def production_add(request):
    _require_owner(request)
    from apps.inventory.models import ProductVariant
    from apps.orders.models import OrderItem

    order_item = None
    order_item_id = request.GET.get("order_item") or request.POST.get("order_item")
    if order_item_id:
        order_item = OrderItem.objects.filter(pk=order_item_id).select_related("variant").first()

    if request.method == "POST":
        variant = get_object_or_404(ProductVariant, pk=request.POST.get("variant"))
        contractor = get_object_or_404(Contractor, pk=request.POST.get("contractor"))
        try:
            accepted_qty = int(request.POST.get("accepted_qty") or 0)
            defect_qty = int(request.POST.get("defect_qty") or 0)
        except ValueError:
            accepted_qty = defect_qty = -1
        material_cost = _parse_decimal(request.POST.get("material_cost")) or Decimal("0")
        trim_cost = _parse_decimal(request.POST.get("trim_cost")) or Decimal("0")
        labor_cost = _parse_decimal(request.POST.get("labor_cost")) or Decimal("0")
        currency = request.POST.get("currency") or "KGS"

        note = (request.POST.get("note") or "").strip()
        if accepted_qty < 0 or defect_qty < 0 or (accepted_qty == 0 and defect_qty == 0):
            messages.error(
                request,
                _("Укажите принятое и/или бракованное количество (хотя бы одно больше нуля)."),
            )
        elif order_item is not None:
            # Launched from an order («Запустить производство») — route
            # through mark_produced so produced_qty/status advance too, the
            # SAME single call apps.orders' own quick «Произведено N шт»
            # action uses, never a second, parallel bookkeeping path.
            from django.core.exceptions import ValidationError

            from apps.orders.services import mark_produced

            try:
                mark_produced(
                    order_item,
                    accepted_qty,
                    user=request.user,
                    defect_qty=defect_qty,
                    contractor=contractor,
                    material_cost=material_cost,
                    trim_cost=trim_cost,
                    labor_cost=labor_cost,
                    currency=currency,
                    note=note,
                )
                messages.success(request, _("Производство записано."))
                return redirect("orders:detail", pk=order_item.order_id)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
        else:
            record_production_run(
                variant=variant,
                contractor=contractor,
                accepted_qty=accepted_qty,
                defect_qty=defect_qty,
                material_cost=material_cost,
                trim_cost=trim_cost,
                labor_cost=labor_cost,
                currency=currency,
                note=note,
                user=request.user,
            )
            messages.success(request, _("Производство записано."))
            return redirect("manufacturing:contractors")

    return render(
        request,
        "manufacturing/production_add.html",
        {
            "variants": ProductVariant.objects.filter(is_active=True).select_related(
                "product__category"
            ),
            "contractors": Contractor.objects.filter(is_active=True).order_by("name"),
            "order_item": order_item,
            "today": timezone.localdate(),
            "active": "manufacturing",
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
