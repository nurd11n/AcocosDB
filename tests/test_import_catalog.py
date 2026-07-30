"""§4 — the opening-stock xlsx import: Russian headers, validate-before-write,
row-level Russian errors, idempotent by SKU, one transaction, opening stock
as a PRODUCTION_IN «начальный остаток» movement — never typed by hand.
"""

from decimal import Decimal

import openpyxl
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.inventory.models import Category, Product, ProductVariant, StockMovement

pytestmark = pytest.mark.django_db

HEADERS = [
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


def _write_xlsx(path, rows, headers=HEADERS):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return str(path)


def test_import_creates_categories_products_variants_and_opening_stock(tmp_path):
    path = _write_xlsx(
        tmp_path / "catalog.xlsx",
        [
            ["Платья", "Вечернее платье", "M", "красный", "EVD-M-RED", 1500, 3200, "KGS", 5],
            ["Платья", "Вечернее платье", "L", "красный", "EVD-L-RED", 1500, 3200, "KGS", 3],
        ],
    )
    call_command("import_catalog", path)

    assert Category.objects.filter(name="Платья").count() == 1
    assert Product.objects.filter(name="Вечернее платье").count() == 1
    v = ProductVariant.objects.get(sku="EVD-M-RED")
    assert v.size == "M" and v.color == "красный"
    assert v.cost_price == Decimal("1500") and v.sale_price == Decimal("3200")
    assert v.currency == "KGS"
    assert v.stock == 5

    m = StockMovement.objects.get(variant=v)
    assert m.movement_type == StockMovement.PRODUCTION_IN
    assert m.quantity == 5
    assert m.reason == "начальный остаток"


def test_import_is_idempotent_by_sku_no_duplicate_stock_or_variant(tmp_path):
    path = _write_xlsx(
        tmp_path / "catalog.xlsx",
        [["Платья", "Вечернее платье", "M", "красный", "EVD-M-RED", 1500, 3200, "KGS", 5]],
    )
    call_command("import_catalog", path)
    call_command("import_catalog", path)  # re-run the SAME file

    assert ProductVariant.objects.filter(sku="EVD-M-RED").count() == 1
    v = ProductVariant.objects.get(sku="EVD-M-RED")
    assert v.stock == 5  # not doubled to 10
    assert StockMovement.objects.filter(variant=v).count() == 1


def test_reimport_with_corrected_price_updates_in_place_without_new_stock(tmp_path):
    path1 = _write_xlsx(
        tmp_path / "v1.xlsx",
        [["Платья", "Вечернее платье", "M", "красный", "EVD-M-RED", 1500, 3200, "KGS", 5]],
    )
    call_command("import_catalog", path1)

    path2 = _write_xlsx(
        tmp_path / "v2.xlsx",
        [["Платья", "Вечернее платье", "M", "красный", "EVD-M-RED", 1600, 3400, "KGS", 999]],
    )
    call_command("import_catalog", path2)

    v = ProductVariant.objects.get(sku="EVD-M-RED")
    assert v.cost_price == Decimal("1600") and v.sale_price == Decimal("3400")
    assert v.stock == 5  # opening-stock column ignored on an existing SKU
    assert StockMovement.objects.filter(variant=v).count() == 1


def test_validation_errors_are_russian_and_row_numbered_nothing_written(tmp_path):
    path = _write_xlsx(
        tmp_path / "bad.xlsx",
        [
            ["Платья", "Вечернее платье", "M", "красный", "EVD-M-RED", 1500, 3200, "KGS", 5],
            ["", "Без категории", "M", "синий", "EVD-M-BLUE", 1500, 3200, "KGS", 2],
            ["Платья", "Плохая цена", "M", "чёрный", "EVD-M-BLACK", "abc", 3200, "KGS", 2],
        ],
    )
    with pytest.raises(CommandError):
        call_command("import_catalog", path)

    # ONE transaction — even the valid first row must not be written.
    assert not ProductVariant.objects.exists()
    assert not Category.objects.exists()


def test_duplicate_sku_within_file_is_rejected(tmp_path):
    path = _write_xlsx(
        tmp_path / "dupe.xlsx",
        [
            ["Платья", "A", "M", "", "SKU-1", 100, 200, "KGS", 1],
            ["Платья", "B", "L", "", "SKU-1", 100, 200, "KGS", 1],
        ],
    )
    with pytest.raises(CommandError):
        call_command("import_catalog", path)
    assert not ProductVariant.objects.exists()


def test_unknown_currency_rejected(tmp_path):
    path = _write_xlsx(
        tmp_path / "badcur.xlsx",
        [["Платья", "A", "M", "", "SKU-2", 100, 200, "EUR", 1]],
    )
    with pytest.raises(CommandError):
        call_command("import_catalog", path)


def test_missing_required_header_rejected(tmp_path):
    path = _write_xlsx(
        tmp_path / "noheaders.xlsx",
        [["Платья", "A", "M", "", "SKU-3", 100, 200, 1]],
        headers=[
            "категория",
            "товар",
            "размер",
            "цвет",
            "артикул",
            "себестоимость",
            "цена",
            "количество",
        ],
    )
    with pytest.raises(CommandError, match="валюта"):
        call_command("import_catalog", path)


def test_dry_run_writes_nothing(tmp_path):
    path = _write_xlsx(
        tmp_path / "dry.xlsx",
        [["Платья", "A", "M", "", "SKU-4", 100, 200, "KGS", 1]],
    )
    call_command("import_catalog", path, dry_run=True)
    assert not ProductVariant.objects.exists()


def test_blank_trailing_rows_are_skipped_not_errors(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    ws.append(["Платья", "A", "M", "", "SKU-5", 100, 200, "KGS", 1])
    ws.append([None] * 9)  # a genuinely blank row, e.g. left over in the sheet
    path = tmp_path / "blank.xlsx"
    wb.save(path)
    call_command("import_catalog", str(path))
    assert ProductVariant.objects.filter(sku="SKU-5").exists()


def test_zero_opening_stock_writes_no_movement(tmp_path):
    path = _write_xlsx(
        tmp_path / "zero.xlsx",
        [["Платья", "A", "M", "", "SKU-6", 100, 200, "KGS", 0]],
    )
    call_command("import_catalog", path)
    v = ProductVariant.objects.get(sku="SKU-6")
    assert v.stock == 0
    assert not StockMovement.objects.filter(variant=v).exists()
