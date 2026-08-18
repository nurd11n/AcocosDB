from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from .models import Contractor, ContractorTransaction, Expense, ProductionRun
from .services import contractor_balance


def _owner_only_permissions(cls):
    """Every manufacturing model is Owner-only — the same tier as cost price
    and profit (apps.core.permissions.can_see_costs), enforced here exactly
    like ExchangeRateAdmin/ClientOpeningBalanceAdmin, never just left to
    hide behind BUSINESS_MODEL_PERMISSIONS not listing these models (that
    alone blocks Editor/Viewer, but a superuser-check here is the same
    defence-in-depth the rest of this codebase applies to every money-
    affecting control)."""

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    cls.has_module_permission = has_module_permission
    cls.has_view_permission = has_view_permission
    cls.has_add_permission = has_add_permission
    cls.has_change_permission = has_change_permission
    cls.has_delete_permission = has_delete_permission
    return cls


@_owner_only_permissions
@admin.register(Contractor)
class ContractorAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["name", "contact", "is_active", "balance_display"]
    list_filter = ["is_active"]
    search_fields = ["name", "contact"]

    @admin.display(description=_("contractor balance"))
    def balance_display(self, obj):
        bal = contractor_balance(obj).get(obj.pk)
        if not bal:
            return "—"
        return ", ".join(f"{amt} {cur}" for cur, amt in bal.items())


@_owner_only_permissions
@admin.register(ContractorTransaction)
class ContractorTransactionAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["contractor", "kind", "amount", "currency", "date", "production_run"]
    list_filter = ["kind", "currency"]
    search_fields = ["contractor__name", "note"]
    date_hierarchy = "date"
    autocomplete_fields = ["contractor"]
    # Set only by apps.manufacturing.services.record_production_run — never
    # hand-picked, same discipline as OrderItem.produced_qty.
    readonly_fields = ["production_run"]


@_owner_only_permissions
@admin.register(ProductionRun)
class ProductionRunAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = [
        "variant",
        "contractor",
        "accepted_qty",
        "defect_qty",
        "success_rate_display",
        "date",
    ]
    list_filter = ["contractor", "date"]
    search_fields = ["variant__sku", "variant__product__name", "contractor__name"]
    date_hierarchy = "date"
    autocomplete_fields = ["variant", "contractor"]

    @admin.display(description=_("success rate"))
    def success_rate_display(self, obj):
        rate = obj.success_rate
        return f"{rate * 100:.0f}%" if rate is not None else "—"


@_owner_only_permissions
@admin.register(Expense)
class ExpenseAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["date", "category", "amount", "currency", "contractor", "variant"]
    list_filter = ["category", "currency"]
    search_fields = ["note", "contractor__name", "variant__sku"]
    date_hierarchy = "date"
    autocomplete_fields = ["contractor", "variant"]
    # Set only by record_production_run — see ContractorTransaction.production_run.
    readonly_fields = ["production_run"]
