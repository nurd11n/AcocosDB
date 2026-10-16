from import_export import fields, resources
from import_export.widgets import ForeignKeyWidget

from apps.reports.export import safe_spreadsheet_text

from .models import Category, Product, ProductVariant


class SafeExportResource(resources.ModelResource):
    def export_field(self, field, instance, **kwargs):
        return safe_spreadsheet_text(super().export_field(field, instance, **kwargs))


class ProductVariantResource(SafeExportResource):
    """Owner export: includes cost price."""

    class Meta:
        model = ProductVariant
        fields = (
            "id",
            "sku",
            "product__name",
            "size",
            "color",
            "currency",
            "cost_price",
            "sale_price",
        )
        export_order = fields


class StaffProductVariantResource(SafeExportResource):
    """Staff export: cost price is Owner-only and must not leak through Excel."""

    class Meta:
        model = ProductVariant
        fields = ("id", "sku", "product__name", "size", "color", "currency", "sale_price")
        export_order = fields


class ProductVariantImportResource(SafeExportResource):
    """Initial catalog import. Columns: sku, category, product, size, color,
    cost_price, sale_price, low_stock_threshold. Creates missing categories/products."""

    product = fields.Field(
        attribute="product", column_name="product", widget=ForeignKeyWidget(Product, "name")
    )

    class Meta:
        model = ProductVariant
        import_id_fields = ("sku",)
        fields = (
            "sku",
            "product",
            "size",
            "color",
            "currency",
            "cost_price",
            "sale_price",
            "low_stock_threshold",
        )

    def before_import_row(self, row, **kwargs):
        name = (row.get("product") or "").strip()
        if not name:
            return
        category, _ = Category.objects.get_or_create(
            name=(row.get("category") or "Uncategorized").strip() or "Uncategorized"
        )
        Product.objects.get_or_create(name=name, defaults={"category": category})
