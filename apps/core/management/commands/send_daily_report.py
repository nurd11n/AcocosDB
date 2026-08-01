"""Daily report: one .xlsx (or four .csv, with --format csv) covering today's
confirmed sales, current stock, outstanding client debts, and today's
unreviewed payments. Emailed to REPORT_RECIPIENTS and sent as a Telegram
document to every BotUser flagged receives_reports=True.

The report is ALWAYS in Russian, regardless of the UI language of whoever
triggers it — sheet names, headers, and the email subject are hardcoded here,
not run through gettext, since the owner reads these regardless of which
language they happen to have the panel set to.

Run manually: python manage.py send_daily_report [--format xlsx|csv]
Scheduled: the `scheduler` container runs this once a day at REPORT_HOUR.
"""

import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.clients.services import client_debts_by_currency, debtors_report_rows
from apps.core.currency import to_base
from apps.core.models import BotUser
from apps.inventory.services import variants_with_stock
from apps.reports import export
from apps.sales.models import Payment, SaleOrder
from apps.sales.services import todays_confirmed_orders

logger = logging.getLogger(__name__)

_PAYMENT_STATUS_RU = {
    SaleOrder.UNPAID: "не оплачено",
    SaleOrder.PARTIAL: "частично",
    SaleOrder.PAID: "оплачено",
}
_RATE_SOURCE_RU = {
    Payment.RATE_NBKR: "НБКР",
    Payment.RATE_MANUAL: "вручную",
}


def _client_debt_lookup() -> dict:
    """{(client_id, currency): debt} — one grouped pair of queries for the
    whole report, so the Sales sheet can show each buyer's TOTAL outstanding
    balance next to the sale without a per-row query."""
    return {
        (cid, cur): amt
        for cid, currs in client_debts_by_currency().items()
        for cur, amt in currs.items()
        if amt > 0
    }


_SALES_COLUMNS = [
    export.Column("№", export.INT),
    export.Column("Время", export.TIME),
    export.Column("Клиент", export.TEXT),
    export.Column("Телефон", export.TEXT),
    export.Column("Канал", export.TEXT),
    export.Column("Позиции", export.TEXT),
    export.Column("Кол-во", export.INT),
    export.Column("Итого", export.MONEY),
    export.Column("Валюта", export.TEXT),
    export.Column(f"Итого, {settings.CURRENCY}", export.MONEY),
    export.Column("Оплачено", export.MONEY),
    export.Column("Долг по продаже", export.MONEY),
    export.Column("Статус оплаты", export.TEXT),
    export.Column("Долг клиента всего", export.MONEY),
    export.Column("Сдача", export.MONEY),
    export.Column("Округление", export.MONEY),
    export.Column("Валюта оплаты", export.TEXT),
    export.Column("Курс", export.TEXT),
    export.Column("Источник курса", export.TEXT),
]


def _sales_sheet() -> export.Sheet:
    today = timezone.localdate()
    orders = list(todays_confirmed_orders())
    debts = _client_debt_lookup()
    rows = []
    revenue_base = Decimal("0")
    collected_base = Decimal("0")
    rounding_total = Decimal("0")
    balance_total = Decimal("0")
    units_total = 0

    for order in orders:
        items = list(order.items.all())
        # paid_amount already converts every payment into the order's OWN
        # currency at the rate frozen on it (see SaleOrder.paid_amount) — a
        # foreign payment must count here exactly as it does in the POS and
        # the client's debt, never a same-currency-only sum.
        paid = order.paid_amount
        balance = order.balance  # todays_confirmed_orders() is always CONFIRMED
        status = _PAYMENT_STATUS_RU[SaleOrder.payment_status_for(order.total, paid)]
        total_base = order.total_kgs  # frozen at confirm — never re-converted
        paid_base = to_base(paid, order.currency, today)

        payments = list(order.payments.all())
        foreign = [p for p in payments if p.currency != order.currency]
        currencies_used = "; ".join(sorted({p.currency for p in payments})) or "—"
        rates_used = "; ".join(sorted({f"{p.rate_to_kgs:.4f}" for p in foreign})) or "—"
        sources_used = "; ".join(sorted({_RATE_SOURCE_RU[p.rate_source] for p in foreign})) or "—"

        # Gross/change/net together (CLAUDE.md): change and its rounding
        # residue, summed across this order's payments, in сом — the residue
        # feeds the day's till-drift total below, never dropped silently.
        change_kgs_total = sum((p.change_amount_kgs for p in payments), Decimal("0"))
        order_rounding_kgs = sum((p.change_rounding_kgs for p in payments), Decimal("0"))
        rounding_total += order_rounding_kgs
        balance_total += balance
        units = sum(i.quantity for i in items)
        units_total += units

        rows.append(
            [
                order.pk,
                timezone.localtime(order.confirmed_at),
                order.client.name if order.client else "без клиента",
                order.client.phone if order.client else "",
                order.get_channel_display(),
                "; ".join(f"{i.variant.sku} ×{i.quantity}" for i in items),
                units,
                order.total,
                order.currency,
                total_base if total_base is not None else "н/д",
                paid,
                balance,
                status,
                debts.get((order.client_id, order.currency)) if order.client_id else None,
                change_kgs_total or None,
                order_rounding_kgs or None,
                currencies_used,
                rates_used,
                sources_used,
            ]
        )
        if total_base is not None:
            revenue_base += total_base
        if paid_base is not None:
            collected_base += paid_base

    totals = [None] * len(_SALES_COLUMNS)
    totals[0] = f"Заказов: {len(orders)}"
    totals[6] = units_total
    totals[9] = revenue_base
    totals[10] = collected_base
    totals[11] = balance_total
    totals[12] = "ИТОГО"
    totals[15] = rounding_total
    return export.Sheet(_SALES_COLUMNS, rows, totals)


_STOCK_COLUMNS = [
    export.Column("Артикул", export.TEXT),
    export.Column("Товар", export.TEXT),
    export.Column("Размер", export.TEXT),
    export.Column("Цвет", export.TEXT),
    export.Column("Остаток", export.INT),
    export.Column("Цена продажи", export.MONEY),
    export.Column("Себестоимость", export.MONEY),
    export.Column("Валюта", export.TEXT),
    export.Column("Стоимость остатка", export.MONEY),
    export.Column(f"Стоимость, {settings.CURRENCY}", export.MONEY),
    export.Column("Низкий остаток", export.TEXT),
]


def _stock_sheet() -> export.Sheet:
    # Cost price + stock value only appear here — this report is owner-directed,
    # unlike the admin panel where Editor/Viewer never see cost.
    today = timezone.localdate()
    rows = []
    units_total = 0
    value_base_total = Decimal("0")
    for v in variants_with_stock():
        stock = v._stock or 0
        value = v.cost_price * stock
        value_base = to_base(value, v.currency, today)
        units_total += stock
        if value_base is not None:
            value_base_total += value_base
        rows.append(
            [
                v.sku,
                v.product.name,
                v.size,
                v.color,
                stock,
                v.sale_price,
                v.cost_price,
                v.currency,
                value,
                value_base if value_base is not None else "н/д",
                "НИЗКИЙ" if stock <= v.low_stock_threshold else "",
            ]
        )
    totals = [None] * len(_STOCK_COLUMNS)
    totals[0] = "ИТОГО"
    totals[4] = units_total
    totals[9] = value_base_total
    return export.Sheet(_STOCK_COLUMNS, rows, totals)


_DEBTS_COLUMNS = [
    export.Column("Клиент", export.TEXT),
    export.Column("Телефон", export.TEXT),
    export.Column("Долг", export.MONEY),
    export.Column("Валюта", export.TEXT),
    export.Column(f"Долг, {settings.CURRENCY}", export.MONEY),
    export.Column("Неоплаченных продаж", export.INT),
    export.Column("Долг с", export.DATE),
    export.Column("Последняя оплата", export.DATE),
]


def _debts_sheet() -> export.Sheet:
    """Who owes what, and — new — how many sales it spans and how long it's
    been outstanding, so the owner can chase the oldest debt first instead of
    just the largest."""
    today = timezone.localdate()
    rows = []
    debt_base_total = Decimal("0")

    # Unpaid-order count + oldest unpaid date per (client, currency), from the
    # confirmed sales themselves — one query, no per-debtor lookup.
    spans: dict[tuple, list] = {}
    for order in SaleOrder.objects.filter(
        status=SaleOrder.CONFIRMED, client__isnull=False
    ).select_related("client"):
        if order.balance <= 0:
            continue
        key = (order.client_id, order.currency)
        bucket = spans.setdefault(key, [0, None])
        bucket[0] += 1
        when = timezone.localtime(order.confirmed_at).date() if order.confirmed_at else None
        if when and (bucket[1] is None or when < bucket[1]):
            bucket[1] = when

    for client, currency, debt, last_payment in debtors_report_rows():
        debt_base = to_base(debt, currency, today)
        if debt_base is not None:
            debt_base_total += debt_base
        count, oldest = spans.get((client.pk, currency), [0, None])
        rows.append(
            [
                client.name,
                client.phone,
                debt,
                currency,
                debt_base if debt_base is not None else "н/д",
                count,
                oldest,
                timezone.localtime(last_payment).date() if last_payment else None,
            ]
        )
    totals = [None] * len(_DEBTS_COLUMNS)
    totals[0] = "ИТОГО"
    totals[4] = debt_base_total
    return export.Sheet(_DEBTS_COLUMNS, rows, totals)


_UNREVIEWED_COLUMNS = [
    export.Column("Время", export.TIME),
    export.Column("Клиент", export.TEXT),
    export.Column("Заказ", export.TEXT),
    export.Column("Сумма", export.MONEY),
    export.Column("Валюта", export.TEXT),
    export.Column("Способ", export.TEXT),
    export.Column("Создал", export.TEXT),
]


def _unreviewed_sheet() -> export.Sheet:
    today = timezone.localdate()
    qs = (
        Payment.objects.filter(created_at__date=today, reviewed=False)
        .select_related("client", "order", "created_by")
        .order_by("created_at")
    )
    rows = [
        [
            timezone.localtime(p.created_at),
            p.client.name if p.client else "—",
            f"#{p.order_id}" if p.order_id else "—",
            p.amount,
            p.currency,
            p.get_method_display(),
            p.created_by.username if p.created_by else "—",
        ]
        for p in qs
    ]
    return export.Sheet(_UNREVIEWED_COLUMNS, rows)


_ORDERS_COLUMNS = [
    export.Column("№", export.INT),
    export.Column("Клиент", export.TEXT),
    export.Column("Телефон", export.TEXT),
    export.Column("Статус", export.TEXT),
    export.Column("Срок", export.DATE),
    export.Column("Просрочен", export.TEXT),
    export.Column("Итого", export.MONEY),
    export.Column("Валюта", export.TEXT),
    export.Column("Аванс", export.MONEY),
    export.Column("Остаток", export.MONEY),
]


def _orders_sheet() -> export.Sheet:
    """Open production orders (CLAUDE.md Part 3g) — due dates, deposits taken,
    and what's still owed. Never includes выдан/отменён — those are closed."""
    from apps.orders.models import Order
    from apps.orders.services import order_paid_amount

    orders = (
        Order.objects.filter(status__in=Order.OPEN_STATUSES)
        .select_related("client")
        .prefetch_related("items", "deposits")
        .order_by("due_date", "-created_at")
    )
    rows = []
    total_sum = Decimal("0")
    paid_sum = Decimal("0")
    remaining_sum = Decimal("0")
    for order in orders:
        paid = order_paid_amount(order)
        remaining = max(order.total - paid, Decimal("0"))
        total_sum += order.total
        paid_sum += paid
        remaining_sum += remaining
        rows.append(
            [
                order.pk,
                order.client.name,
                order.client.phone,
                order.get_status_display(),
                order.due_date,
                "ПРОСРОЧЕН" if order.is_overdue else "",
                order.total,
                order.currency,
                paid,
                remaining,
            ]
        )
    totals = [None] * len(_ORDERS_COLUMNS)
    totals[0] = f"Заказов: {len(rows)}"
    totals[5] = "ИТОГО"
    totals[6] = total_sum
    totals[8] = paid_sum
    totals[9] = remaining_sum
    return export.Sheet(_ORDERS_COLUMNS, rows, totals)


def _sheets() -> dict[str, export.Sheet]:
    return {
        "Продажи": _sales_sheet(),
        "Остаток": _stock_sheet(),
        "Долги": _debts_sheet(),
        "Не проверено": _unreviewed_sheet(),
        "Заказы": _orders_sheet(),
    }


def _build_xlsx() -> bytes:
    return export.to_xlsx(_sheets())


_CSV_FILENAMES = {
    "Продажи": "prodazhi",
    "Остаток": "ostatok",
    "Долги": "dolgi",
    "Не проверено": "ne_provereno",
    "Заказы": "zakazy",
}


def _build_csvs() -> dict[str, bytes]:
    return {
        f"acocos_{_CSV_FILENAMES[name]}.csv": export.to_csv_bytes(sheet)
        for name, sheet in _sheets().items()
    }


def _send_telegram_document(token: str, chat_id: int, filename: str, content: bytes) -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        requests.post(
            url, data={"chat_id": chat_id}, files={"document": (filename, content)}, timeout=30
        )
    except requests.RequestException:
        logger.exception("Failed to send report to Telegram chat %s", chat_id)


class Command(BaseCommand):
    help = "Build and send the daily sales/stock/debts report (email + Telegram)."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")

    def handle(self, *args, **options):
        today = timezone.localdate()
        today_iso = today.isoformat()
        today_ru = today.strftime("%d.%m.%Y")
        if options["format"] == "xlsx":
            files = {f"acocos_report_{today_iso}.xlsx": _build_xlsx()}
        else:
            files = _build_csvs()

        if settings.REPORT_RECIPIENTS:
            email = EmailMessage(
                subject=f"Отчёт ACOCOS за {today_ru}",
                body=f"Отчёт за {today_ru} во вложении.",
                to=settings.REPORT_RECIPIENTS,
            )
            for filename, content in files.items():
                email.attach(filename, content)
            email.send(fail_silently=False)
            self.stdout.write(
                self.style.SUCCESS(f"Emailed report to {', '.join(settings.REPORT_RECIPIENTS)}")
            )
        else:
            self.stdout.write(self.style.WARNING("REPORT_RECIPIENTS is empty — email skipped."))

        # BotUser is the staff allowlist — the report goes out on the staff bot,
        # never the public client bot. BOTS_ENABLED=False (the shipped prod
        # default — bots aren't production-ready) skips this block entirely;
        # email delivery above is unaffected.
        if not settings.BOTS_ENABLED:
            self.stdout.write(self.style.WARNING("BOTS_ENABLED=False — Telegram delivery skipped."))
            return
        token = settings.TELEGRAM_STAFF_TOKEN
        recipients = list(BotUser.objects.filter(is_active=True, receives_reports=True))
        if token and recipients:
            for user in recipients:
                for filename, content in files.items():
                    _send_telegram_document(token, user.telegram_id, filename, content)
            self.stdout.write(
                self.style.SUCCESS(f"Sent report to {len(recipients)} Telegram user(s).")
            )
        elif not token:
            self.stdout.write(
                self.style.WARNING("TELEGRAM_STAFF_TOKEN is empty — Telegram delivery skipped.")
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No BotUser has receives_reports=True — Telegram delivery skipped."
                )
            )
