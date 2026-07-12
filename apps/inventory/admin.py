from django.contrib import admin
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from import_export.admin import ExportActionModelAdmin, ImportMixin
from simple_history.admin import SimpleHistoryAdmin
from unfold.admin import ModelAdmin, TabularInline

from apps.core.permissions import can_see_costs

from .models import Category, Product, ProductVariant, StockMovement
from .resources import (
    ProductVariantImportResource,
    ProductVariantResource,
    StaffProductVariantResource,
)
from .services import add_movement


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    search_fields = ["name"]


class VariantInline(TabularInline):
    model = ProductVariant
    extra = 0
    fields = ["sku", "size", "color", "cost_price", "sale_price", "low_stock_threshold", "is_active"]

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if not can_see_costs(request.user):
            fields = [f for f in fields if f != "cost_price"]
        return fields


@admin.register(Product)
class ProductAdmin(SimpleHistoryAdmin, ModelAdmin):
    list_display = ["name", "category", "is_active", "created_at"]
    list_filter = ["category", "is_active"]
    search_fields = ["name"]
    inlines = [VariantInline]
    list_select_related = ["category"]


@admin.register(ProductVariant)
class ProductVariantAdmin(SimpleHistoryAdmin, ImportMixin, ExportActionModelAdmin, ModelAdmin):
    list_display = ["sku", "product", "size", "color", "current_stock", "sale_price", "is_active"]
    list_filter = ["is_active", "product__category"]
    search_fields = ["sku", "product__name", "color"]

    def get_export_resource_classes(self, request):
        if can_see_costs(request.user):
            return [ProductVariantResource]
        return [StaffProductVariantResource]

    def get_import_resource_classes(self, request):
        return [ProductVariantImportResource]

    def has_import_permission(self, request):
        # Imports carry cost prices — Owner only.
        return can_see_costs(request.user)

    def get_queryset(self, request):
        # One query for the whole list page: join product, aggregate stock in SQL.
        qs = super().get_queryset(request).select_related("product")
        return qs.annotate(_stock=Sum("movements__quantity"))

    @admin.display(description=_("stock"), ordering="_stock")
    def current_stock(self, obj):
        return obj._stock or 0

    def get_list_display(self, request):
        cols = list(super().get_list_display(request))
        if can_see_costs(request.user):
            cols.insert(cols.index("sale_price"), "cost_price")
        return cols

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if not can_see_costs(request.user):
            fields = [f for f in fields if f != "cost_price"]
        return fields


@admin.register(StockMovement)
class StockMovementAdmin(ModelAdmin):
    """The ledger is append-only: rows can be added, never edited or deleted."""

    list_display = ["created_at", "variant", "movement_type", "quantity", "reason", "created_by"]
    list_filter = ["movement_type"]
    search_fields = ["variant__sku", "variant__product__name", "reason"]
    list_select_related = ["variant__product", "created_by"]
    fields = ["variant", "movement_type", "quantity", "reason"]
    autocomplete_fields = ["variant"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        qty = abs(obj.quantity)
        if obj.movement_type in StockMovement.OUT_TYPES:
            qty = -qty
        obj.quantity = qty
        obj.created_by = request.user
        obj.full_clean()
        super().save_model(request, obj, form, change)
