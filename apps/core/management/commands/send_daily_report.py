"""Daily report: one .xlsx (or three .csv, with --format csv) covering today's
confirmed sales, current stock, and outstanding client debts. Emailed to
REPORT_RECIPIENTS and sent as a Telegram document to every BotUser flagged
receives_reports=True. Email is the archive; Telegram is what actually gets read.

Run manually: python manage.py send_daily_report [--format xlsx|csv]
Scheduled: the `scheduler` container runs this once a day at REPORT_HOUR.
"""

import csv
import io
import logging

import requests
from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone
from openpyxl import Workbook

from apps.clients.services import debtors_report_rows
from apps.core.models import BotUser
from apps.inventory.services import variants_with_stock
from apps.sales.services import todays_confirmed_orders

logger = logging.getLogger(__name__)


def _sales_rows() -> list[list]:
    rows = [["Time", "Client", "Channel", "Items", "Quantities", "Unit Prices", "Order Total"]]
    orders = list(todays_confirmed_orders())
    revenue = 0
    for order in orders:
        items = list(order.items.all())
        rows.append(
            [
                timezone.localtime(order.confirmed_at).strftime("%H:%M"),
                order.client.name if order.client else "walk-in",
                order.get_channel_display(),
                "; ".join(i.variant.sku for i in items),
                "; ".join(str(i.quantity) for i in items),
                "; ".join(str(i.unit_price) for i in items),
                str(order.total),
            ]
        )
        revenue += order.total
    rows.append(["", "", "", "", "", f"Orders: {len(orders)}", f"Revenue: {revenue}"])
    return rows


def _stock_rows() -> list[list]:
    # Cost price + stock value only appear here — this report is owner-directed,
    # unlike the admin panel where Editor/Viewer never see cost.
    rows = [
        [
            "SKU",
            "Product",
            "Size",
            "Color",
            "Stock",
            "Sale Price",
            "Cost Price",
            "Stock Value",
            "Low",
        ]
    ]
    for v in variants_with_stock():
        stock = v._stock or 0
        rows.append(
            [
                v.sku,
                v.product.name,
                v.size,
                v.color,
                stock,
                str(v.sale_price),
                str(v.cost_price),
                str(v.cost_price * stock),
                "LOW" if stock <= v.low_stock_threshold else "",
            ]
        )
    return rows


def _debts_rows() -> list[list]:
    rows = [["Name", "Phone", "Debt", "Last Payment Date"]]
    for client, debt, last_payment in debtors_report_rows():
        rows.append(
            [
                client.name,
                client.phone,
                str(debt),
                timezone.localtime(last_payment).strftime("%Y-%m-%d") if last_payment else "",
            ]
        )
    return rows


def _build_xlsx() -> bytes:
    wb = Workbook()
    sheets = {"Sales": _sales_rows(), "Stock": _stock_rows(), "Debts": _debts_rows()}
    for i, (name, rows) in enumerate(sheets.items()):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = name
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_csvs() -> dict[str, bytes]:
    sheets = {"sales": _sales_rows(), "stock": _stock_rows(), "debts": _debts_rows()}
    files = {}
    for name, rows in sheets.items():
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        # UTF-8 BOM — plain UTF-8 CSV shows broken Cyrillic when opened in Excel.
        files[f"acocos_{name}.csv"] = ("﻿" + buf.getvalue()).encode("utf-8")
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
        today = timezone.localdate().isoformat()
        if options["format"] == "xlsx":
            files = {f"acocos_report_{today}.xlsx": _build_xlsx()}
        else:
            files = _build_csvs()

        if settings.REPORT_RECIPIENTS:
            email = EmailMessage(
                subject=f"ACOCOS daily report — {today}",
                body=f"Daily report for {today} attached.",
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

        token = settings.TELEGRAM_BOT_TOKEN
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
                self.style.WARNING("TELEGRAM_BOT_TOKEN is empty — Telegram delivery skipped.")
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No BotUser has receives_reports=True — Telegram delivery skipped."
                )
            )
