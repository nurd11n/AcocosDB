"""Storage / inventory dashboard aggregates + spreadsheet builders.

Every figure comes from the SAME append-only StockMovement ledger the rest of
the system trusts (stock = Σ quantity), pivoted by movement type into the
columns an owner thinks in: поступление (intake), продано (sold), возвраты
(returns), брак/списание (defective write-offs), корректировки (adjustments),
and the current остаток (stock). One grouped query does the whole per-variant
pivot — no N+1, no catalog-size explosion — and the category rollup + totals
are summed in Python from those rows.

This module is the ONE place the numbers live: the /storage/ page and the
xlsx/csv export both render from storage_data(), so the screen and the download
can never disagree. Cost price and stock value appear here because the whole
surface is Owner-only (see apps/reports/views.py).
"""

from decimal import Decimal

from django.conf import settings
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from apps.core.currency import to_base
from apps.inventory.models import ProductVariant, StockMovement

from . import export

CENTS = Decimal("0.01")


def _pivot():
    """All active variants with each movement type summed in one grouped query.
    Coalesce keeps a variant with zero movements in the result (stock 0), and a
    LEFT JOIN + conditional Sum means this is a single query for the whole
    catalog, never one per variant."""
    intake_types = [StockMovement.PRODUCTION_IN, StockMovement.PURCHASE_IN]

    def _sum(**flt):
        return Coalesce(Sum("movements__quantity", filter=Q(**flt)), 0)

    return (
        ProductVariant.objects.filter(is_active=True)
        .select_related("product__category")
        .annotate(
            _intake=_sum(movements__movement_type__in=intake_types),
            _returns=_sum(movements__movement_type=StockMovement.RETURN_IN),
            _sold=_sum(movements__movement_type=StockMovement.SALE_OUT),
            _writeoff=_sum(movements__movement_type=StockMovement.WRITEOFF_OUT),
            _adjust=_sum(movements__movement_type=StockMovement.ADJUSTMENT),
            _stock=Coalesce(Sum("movements__quantity"), 0),
        )
        .order_by("product__category__name", "product__name", "sku")
    )


def storage_data() -> dict:
    """{summary, categories, items} for the storage dashboard and its export."""
    from django.utils import timezone

    today = timezone.localdate()
    items = []
    for v in _pivot():
        stock = v._stock or 0
        # sale_out / writeoff_out are stored negative; show them as positive
        # counts (units that left), keeping adjustments signed and stock net.
        sold = -(v._sold or 0)
        writeoff = -(v._writeoff or 0)
        value = (v.cost_price or Decimal("0")) * stock
        value_base = to_base(value, v.currency, today)
        items.append(
            {
                "sku": v.sku,
                "product": v.product.name,
                "category": v.product.category.name if v.product.category_id else "—",
                "size": v.size,
                "color": v.color,
                "currency": v.currency,
                "intake": v._intake or 0,
                "returns": v._returns or 0,
                "sold": sold,
                "writeoff": writeoff,
                "adjust": v._adjust or 0,
                "stock": stock,
                "low": stock <= v.low_stock_threshold,
                "value": value,
                "value_base": value_base,
            }
        )

    # Category rollup + grand totals, summed from the item rows (no extra query).
    cats: dict[str, dict] = {}
    summary = {
        "variants": 0,
        "in_stock": 0,
        "units": 0,
        "intake": 0,
        "returns": 0,
        "sold": 0,
        "writeoff": 0,
        "low": 0,
        "value_base": Decimal("0"),
    }
    for it in items:
        c = cats.setdefault(
            it["category"],
            {
                "name": it["category"],
                "variants": 0,
                "units": 0,
                "intake": 0,
                "sold": 0,
                "returns": 0,
                "writeoff": 0,
                "value_base": Decimal("0"),
            },
        )
        c["variants"] += 1
        c["units"] += it["stock"]
        c["intake"] += it["intake"]
        c["sold"] += it["sold"]
        c["returns"] += it["returns"]
        c["writeoff"] += it["writeoff"]

        summary["variants"] += 1
        summary["units"] += it["stock"]
        summary["intake"] += it["intake"]
        summary["returns"] += it["returns"]
        summary["sold"] += it["sold"]
        summary["writeoff"] += it["writeoff"]
        if it["stock"] > 0:
            summary["in_stock"] += 1
        if it["low"]:
            summary["low"] += 1
        if it["value_base"] is not None:
            summary["value_base"] += it["value_base"]
            c["value_base"] += it["value_base"]

    categories = sorted(cats.values(), key=lambda c: c["units"], reverse=True)
    return {"summary": summary, "categories": categories, "items": items}


# --- Spreadsheet builders — Russian headers, same rows for xlsx and csv --------

_ITEM_HEADERS = [
    "Категория",
    "Артикул",
    "Товар",
    "Размер",
    "Цвет",
    "Поступление",
    "Продано",
    "Возвраты",
    "Брак/списание",
    "Корректировки",
    "Остаток",
    "Валюта",
    "Себестоимость остатка",
    "≈ Стоимость, " + settings.CURRENCY,
    "Низкий остаток",
]
_CATEGORY_HEADERS = [
    "Категория",
    "Позиций",
    "Поступление",
    "Продано",
    "Возвраты",
    "Брак/списание",
    "Остаток",
    "≈ Стоимость, " + settings.CURRENCY,
]


def _fmt_base(value) -> str:
    return str(value.quantize(CENTS)) if value is not None else "н/д"


def storage_sheets() -> dict[str, list[list]]:
    data = storage_data()
    items = [_ITEM_HEADERS]
    for it in data["items"]:
        items.append(
            [
                it["category"],
                it["sku"],
                it["product"],
                it["size"],
                it["color"],
                it["intake"],
                it["sold"],
                it["returns"],
                it["writeoff"],
                it["adjust"],
                it["stock"],
                it["currency"],
                str(it["value"]),
                _fmt_base(it["value_base"]),
                "да" if it["low"] else "",
            ]
        )
    categories = [_CATEGORY_HEADERS]
    for c in data["categories"]:
        categories.append(
            [
                c["name"],
                c["variants"],
                c["intake"],
                c["sold"],
                c["returns"],
                c["writeoff"],
                c["units"],
                _fmt_base(c["value_base"]),
            ]
        )
    return {"Товары": items, "Категории": categories}


def build_xlsx() -> bytes:
    return export.to_xlsx(storage_sheets())


def build_csvs() -> dict[str, bytes]:
    """{stub: bytes} — one flat CSV per sheet, UTF-8 with BOM for Excel."""
    stub = {"Товары": "sklad_tovary", "Категории": "sklad_kategorii"}
    return {stub[name]: export.to_csv_bytes(rows) for name, rows in storage_sheets().items()}
