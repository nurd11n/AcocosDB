"""/personal/ — Owner-only, same gate pattern as apps.manufacturing.views
(explicit is_superuser check here, never just left to hide behind
BUSINESS_MODEL_PERMISSIONS not listing this app's models — that alone
blocks Editor/Viewer, but a view-level check is the same defence-in-depth
every other money-affecting control in this codebase applies)."""

from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.currency import format_money, rate_info
from apps.pos.decorators import pos_view
from apps.reports import export

from .models import PersonalExpense
from .services import record_personal_expense


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
def expenses(request):
    _require_owner(request)
    if request.method == "POST":
        amount = _parse_decimal(request.POST.get("amount"))
        tag = request.POST.get("tag")
        currency = request.POST.get("currency") or settings.CURRENCY
        entry_date = _parse_date(request.POST.get("date")) or timezone.localdate()
        # Same rule apps.manufacturing.views.expenses and record_payment
        # already enforce: a foreign-currency amount with NO rate on record
        # must save NOTHING, never fall back to snapshot_rate_to_base's
        # silent 1.0 — that would file 10 $ as 10 сом, understating it ~87x
        # with no warning.
        missing_rate = currency != settings.CURRENCY and rate_info(currency) is None
        if not amount or amount <= 0:
            messages.error(request, _("Укажите сумму больше нуля."))
        elif tag not in dict(PersonalExpense.TAG_CHOICES):
            messages.error(request, _("Некорректный тег."))
        elif missing_rate:
            messages.error(
                request,
                _("Нет курса для %(c)s — расход не сохранён. Обновите курс и повторите.")
                % {"c": currency},
            )
        else:
            expense = record_personal_expense(
                date=entry_date,
                tag=tag,
                amount=amount,
                currency=currency,
                description=(request.POST.get("description") or "").strip(),
            )
            # Confirm what was ACTUALLY filed, in both the typed currency and
            # сом — same reasoning as apps.manufacturing.views.expenses: a
            # mis-picked currency is otherwise invisible until it has already
            # inflated every total on this page.
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
            return redirect("personal:expenses")

    from .dashboard import personal_expenses_by_tag

    rows = PersonalExpense.objects.all()[:100]
    return render(
        request,
        "personal/expenses.html",
        {
            "rows": rows,
            "tags": PersonalExpense.TAG_CHOICES,
            # All-time, same reasoning as apps.manufacturing.views.expenses:
            # this page has no period filter of its own.
            "by_tag": personal_expenses_by_tag(date_cls(2000, 1, 1), timezone.localdate()),
            "today": timezone.localdate(),
            # So the list can show «≈ N сом» beside a non-KGS row (CLAUDE.md's
            # money rule) — this list mixes currencies under сом-converted totals.
            "base_currency": settings.CURRENCY,
            "active": "personal",
        },
    )


@pos_view
def expenses_export(request):
    """«Личные расходы» — a download of ITS OWN, never a sheet inside
    apps.core.management.commands.send_daily_report or
    apps.reports.dashboard's export (see apps.personal.models's own
    docstring for why that separation matters)."""
    _require_owner(request)
    labels = dict(PersonalExpense.TAG_CHOICES)
    sheet = export.Sheet(
        columns=[
            export.Column("Дата", export.DATE),
            export.Column("Тег"),
            export.Column("Сумма"),
            export.Column("Валюта"),
            export.Column("Сумма, сом", export.MONEY),
            export.Column("Описание"),
        ],
        rows=[
            [
                e.date,
                labels[e.tag],
                e.amount,
                e.currency,
                e.amount_kgs,
                e.description,
            ]
            for e in PersonalExpense.objects.all()
        ],
    )
    today = timezone.localdate().isoformat()
    content = export.to_csv_bytes(sheet)
    resp = HttpResponse(content, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="lichnye_raskhody_{today}.csv"'
    return resp
