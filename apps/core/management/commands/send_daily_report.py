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

import csv
import io
import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone
from openpyxl import Workbook

from apps.clients.services import debtors_report_rows
from apps.core.currency import to_base
from apps.core.models import BotUser
from apps.inventory.services import variants_with_stock
from apps.sales.models import Payment, SaleOrder
from apps.sales.services import todays_confirmed_orders

logger = logging.getLogger(__name__)

_PAYMENT_STATUS_RU = {
    SaleOrder.UNPAID: "не оплачено",
    SaleOrder.PARTIAL: "частично",
    SaleOrder.PAID: "оплачено",
}


def _sales_rows() -> list[list]:
    rows = [
        [
            "Время",
            "Клиент",
            "Канал",
            "Товары",
            "Кол-во",
            "Цена за ед.",
            "Итого",
            "Валюта",
            "≈ Итого, " + settings.CURRENCY,
            "Оплачено",
            "Остаток",
            "Статус оплаты",
        ]
    ]
    today = timezone.localdate()
    orders = list(todays_confirmed_orders())
    orders_count = len(orders)
    revenue_base = Decimal("0")
    collected_base = Decimal("0")
    for order in orders:
        items = list(order.items.all())
        paid = sum(
            (p.amount for p in order.payments.all() if p.currency == order.currency), Decimal("0")
        )
        balance = max(order.total - paid, Decimal("0"))
        status = _PAYMENT_STATUS_RU[SaleOrder.payment_status_for(order.total, paid)]
        total_base = order.total_kgs  # frozen at confirm — never re-converted
        paid_base = to_base(paid, order.currency, today)
        rows.append(
            [
                timezone.localtime(order.confirmed_at).strftime("%H:%M"),
                order.client.name if order.client else "без клиента",
                order.get_channel_display(),
                "; ".join(i.variant.sku for i in items),
                "; ".join(str(i.quantity) for i in items),
                "; ".join(str(i.unit_price) for i in items),
                str(order.total),
                order.currency,
                str(total_base) if total_base is not None else "н/д",
                str(paid),
                str(balance),
                status,
            ]
        )
        if total_base is not None:
            revenue_base += total_base
        if paid_base is not None:
            collected_base += paid_base
    rows.append(
        [
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            f"Заказов: {orders_count}; Выручка ≈{revenue_base} {settings.CURRENCY}",
            f"Получено ≈{collected_base} {settings.CURRENCY}",
            f"Долг ≈{revenue_base - collected_base} {settings.CURRENCY}",
            "",
        ]
    )
    return rows


def _stock_rows() -> list[list]:
    # Cost price + stock value only appear here — this report is owner-directed,
    # unlike the admin panel where Editor/Viewer never see cost.
    rows = [
        [
            "Артикул",
            "Товар",
            "Размер",
            "Цвет",
            "Остаток",
            "Цена продажи",
            "Себестоимость",
            "Валюта",
            "Стоимость остатка",
            "≈ Стоимость, " + settings.CURRENCY,
            "Низкий остаток",
        ]
    ]
    today = timezone.localdate()
    for v in variants_with_stock():
        stock = v._stock or 0
        value = v.cost_price * stock
        value_base = to_base(value, v.currency, today)
        rows.append(
            [
                v.sku,
                v.product.name,
                v.size,
                v.color,
                stock,
                str(v.sale_price),
                str(v.cost_price),
                v.currency,
                str(value),
                str(value_base) if value_base is not None else "н/д",
                "НИЗКИЙ" if stock <= v.low_stock_threshold else "",
            ]
        )
    return rows


def _debts_rows() -> list[list]:
    rows = [
        [
            "Имя",
            "Телефон",
            "Долг",
            "Валюта",
            "≈ Долг, " + settings.CURRENCY,
            "Дата последней оплаты",
        ]
    ]
    today = timezone.localdate()
    for client, currency, debt, last_payment in debtors_report_rows():
        debt_base = to_base(debt, currency, today)
        rows.append(
            [
                client.name,
                client.phone,
                str(debt),
                currency,
                str(debt_base) if debt_base is not None else "н/д",
                timezone.localtime(last_payment).strftime("%Y-%m-%d") if last_payment else "",
            ]
        )
    return rows


def _unreviewed_rows() -> list[list]:
    rows = [["Время", "Клиент", "Заказ", "Сумма", "Валюта", "Способ", "Создал"]]
    today = timezone.localdate()
    qs = (
        Payment.objects.filter(created_at__date=today, reviewed=False)
        .select_related("client", "order", "created_by")
        .order_by("created_at")
    )
    for p in qs:
        rows.append(
            [
                timezone.localtime(p.created_at).strftime("%H:%M"),
                p.client.name if p.client else "—",
                f"#{p.order_id}" if p.order_id else "—",
                str(p.amount),
                p.currency,
                p.get_method_display(),
                p.created_by.username if p.created_by else "—",
            ]
        )
    return rows


def _sheets() -> dict[str, list[list]]:
    return {
        "Продажи": _sales_rows(),
        "Остаток": _stock_rows(),
        "Долги": _debts_rows(),
        "Не проверено": _unreviewed_rows(),
    }


def _build_xlsx() -> bytes:
    wb = Workbook()
    for i, (name, rows) in enumerate(_sheets().items()):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = name
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_CSV_FILENAMES = {
    "Продажи": "prodazhi",
    "Остаток": "ostatok",
    "Долги": "dolgi",
    "Не проверено": "ne_provereno",
}


def _build_csvs() -> dict[str, bytes]:
    files = {}
    for name, rows in _sheets().items():
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        # UTF-8 BOM — plain UTF-8 CSV shows broken Cyrillic when opened in Excel.
        files[f"acocos_{_CSV_FILENAMES[name]}.csv"] = ("﻿" + buf.getvalue()).encode("utf-8")
    return files


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
        # never the public client bot.
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
