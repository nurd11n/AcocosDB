"""One-time (or repeatable) catalog import from an .xlsx price list — the
data-migration path for a business that already has stock before going live
on ACOCOS.

Expected header row (exact Russian column names, any order, row 1):
  категория | товар | размер | цвет | артикул | себестоимость | цена | валюта | количество

- размер/цвет may be blank (not every item has a size/colour).
- артикул (SKU) is the identity key: re-running the SAME file is a no-op
  beyond correcting category/name/prices — it never re-adds opening stock or
  creates a duplicate variant (idempotent by SKU).
- Every row is validated FIRST; any bad row aborts the WHOLE import with a
  Russian, row-numbered error list — nothing is written on a partial failure.
- A genuinely NEW SKU's «количество» becomes its opening stock, written as
  ONE PRODUCTION_IN movement with reason «начальный остаток» — never typed
  into ProductVariant by hand, same rule as every other stock change
  (CLAUDE.md: "Do NOT type stock ... by hand").
- The whole import (products, variants, opening-stock movements) is ONE
  atomic transaction: a bad row 400 out of 500 rolls back rows 1-399 too.

Run: python manage.py import_catalog catalog.xlsx [--dry-run]
"""

from decimal import Decimal, InvalidOperation

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.currency import CURRENCY_CODES
from apps.inventory.models import Category, Product, ProductVariant, StockMovement
from apps.inventory.services import add_movement

REQUIRED_HEADERS = [
    "категория",
    "товар",
    "размер",
    "цвет",
    "артикул",
    "себестоимость",
    "цена",
    "валюта",
    "количество",
]
OPTIONAL_HEADERS = {"размер", "цвет"}


def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def _decimal(value):
    """openpyxl hands back float/int/str/None for a numeric cell — Decimal
    money is non-negotiable (CLAUDE.md), so always convert via str(), never
    Decimal(float) which imports float's own binary rounding error."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        return None


class Command(BaseCommand):
    help = "Import (or re-sync) the product catalog from an .xlsx price list."

    def add_arguments(self, parser):
        parser.add_argument("xlsx_path")
        parser.add_argument("--dry-run", action="store_true", help="Validate only, write nothing.")

    def handle(self, *args, **options):
        path = options["xlsx_path"]
        try:
            wb = openpyxl.load_workbook(path, data_only=True)
        except FileNotFoundError as exc:
            raise CommandError(f"Файл не найден: {path}") from exc
        except Exception as exc:  # corrupt/non-xlsx file
            raise CommandError(f"Не удалось открыть файл: {exc}") from exc

        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise CommandError("Файл пуст.")

        header_row = [_clean(c).lower() for c in rows[0]]
        col_index = {
            name: header_row.index(name) for name in REQUIRED_HEADERS if name in header_row
        }
        missing_headers = [h for h in REQUIRED_HEADERS if h not in col_index]
        if missing_headers:
            raise CommandError("В файле отсутствуют колонки: " + ", ".join(missing_headers))

        errors: list[str] = []
        parsed_rows: list[dict] = []
        seen_skus: dict[str, int] = {}

        for i, raw_row in enumerate(rows[1:], start=2):
            if raw_row is None or all(c is None or _clean(c) == "" for c in raw_row):
                continue  # a trailing/blank row — skip silently, not an error

            def cell(name):
                idx = col_index[name]
                return raw_row[idx] if idx < len(raw_row) else None

            category_name = _clean(cell("категория"))
            product_name = _clean(cell("товар"))
            size = _clean(cell("размер")) if "размер" in col_index else ""
            color = _clean(cell("цвет")) if "цвет" in col_index else ""
            sku = _clean(cell("артикул"))
            cost_price = _decimal(cell("себестоимость"))
            sale_price = _decimal(cell("цена"))
            currency = _clean(cell("валюта")).upper()
            quantity_raw = cell("количество")

            row_errors = []
            if not category_name:
                row_errors.append("пустая категория")
            if not product_name:
                row_errors.append("пустой товар")
            if not sku:
                row_errors.append("пустой артикул")
            elif sku in seen_skus:
                row_errors.append(f"артикул «{sku}» уже встречался в строке {seen_skus[sku]}")
            if cost_price is None or cost_price < 0:
                row_errors.append("себестоимость должна быть числом ≥ 0")
            if sale_price is None or sale_price < 0:
                row_errors.append("цена должна быть числом ≥ 0")
            if currency not in CURRENCY_CODES:
                row_errors.append(
                    f"неизвестная валюта «{currency}» (ожидается {', '.join(CURRENCY_CODES)})"
                )
            quantity = None
            if quantity_raw is None or _clean(quantity_raw) == "":
                row_errors.append("пустое количество")
            else:
                try:
                    quantity = int(quantity_raw)
                    if quantity < 0:
                        row_errors.append("количество не может быть отрицательным")
                except (TypeError, ValueError):
                    row_errors.append("количество должно быть целым числом")

            if row_errors:
                errors.append(f"Строка {i}: " + "; ".join(row_errors) + ".")
                continue

            seen_skus[sku] = i
            parsed_rows.append(
                {
                    "row": i,
                    "category": category_name,
                    "product": product_name,
                    "size": size,
                    "color": color,
                    "sku": sku,
                    "cost_price": cost_price,
                    "sale_price": sale_price,
                    "currency": currency,
                    "quantity": quantity,
                }
            )

        if errors:
            self.stderr.write(self.style.ERROR(f"{len(errors)} ошибок — ничего не записано:"))
            for e in errors:
                self.stderr.write(f"  {e}")
            raise CommandError("Импорт остановлен из-за ошибок в файле.")

        if not parsed_rows:
            self.stdout.write(self.style.WARNING("Нет строк для импорта."))
            return

        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(f"Проверка пройдена: {len(parsed_rows)} строк(и), ошибок нет.")
            )
            return

        created_products = created_variants = updated_variants = 0
        stock_written = 0

        with transaction.atomic():
            for data in parsed_rows:
                category, _c = Category.objects.get_or_create(name=data["category"])
                product, product_created = Product.objects.get_or_create(
                    category=category, name=data["product"]
                )
                created_products += int(product_created)

                variant = ProductVariant.objects.filter(sku=data["sku"]).first()
                if variant is None:
                    variant = ProductVariant.objects.create(
                        product=product,
                        sku=data["sku"],
                        size=data["size"],
                        color=data["color"],
                        currency=data["currency"],
                        cost_price=data["cost_price"],
                        sale_price=data["sale_price"],
                    )
                    created_variants += 1
                    if data["quantity"] > 0:
                        add_movement(
                            variant=variant,
                            movement_type=StockMovement.PRODUCTION_IN,
                            quantity=data["quantity"],
                            reason="начальный остаток",
                        )
                        stock_written += 1
                else:
                    # Idempotent re-run: correct product/size/color/prices in
                    # place, but NEVER re-add opening stock for an SKU that
                    # already has a ledger — that would double the count.
                    variant.product = product
                    variant.size = data["size"]
                    variant.color = data["color"]
                    variant.currency = data["currency"]
                    variant.cost_price = data["cost_price"]
                    variant.sale_price = data["sale_price"]
                    variant.save(
                        update_fields=[
                            "product",
                            "size",
                            "color",
                            "currency",
                            "cost_price",
                            "sale_price",
                        ]
                    )
                    updated_variants += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Готово: {created_products} новых товаров, {created_variants} новых "
                f"вариантов ({stock_written} с начальным остатком), {updated_variants} "
                f"обновлено."
            )
        )
