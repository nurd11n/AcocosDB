from django.conf import settings
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from apps.core.currency import format_money, snapshot_rate_to_base

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


class _FrozenRateAdminMixin:
    """rate_to_kgs is a CONSEQUENCE, never typed — the same rule services.py
    already follows (snapshot_rate_to_base freezes it once, at creation, and
    nothing ever recomputes it).

    Both money models here declare `rate_to_kgs` with no default, so leaving
    it out of the admin made it a REQUIRED, hand-typed field on the /panel/
    add form. A сом amount filed there with a сом-per-dollar rate typed in
    (88) was stored as amount × 88 — 181 000 сом counted as 15 928 000 in
    every total, which is exactly how this was found. CLAUDE.md's rule is
    explicit: money figures are never typed by hand, and a non-Owner may
    never set an exchange rate; a hand-typed rate on an Owner-only form is
    the same defect wearing a different hat.

    Readonly here, frozen on ADD from the currency + date actually chosen,
    and never touched again on edit (a filed row must not follow today's
    rate — same freeze discipline as Payment.rate_to_kgs)."""

    readonly_fields_extra = ("rate_to_kgs", "amount_kgs_display")

    @admin.display(description=_("in KGS (computed)"))
    def amount_kgs_display(self, obj):
        if obj is None or obj.pk is None:
            return _("— computed on save from the currency and date chosen")
        return format_money(obj.amount_kgs, settings.CURRENCY)

    def save_model(self, request, obj, form, change):
        # Freeze ONCE, on creation only. An edit keeps the original rate:
        # re-deriving it here would silently restate a past expense every
        # time someone fixed a typo in its note.
        if not change:
            obj.rate_to_kgs = snapshot_rate_to_base(obj.currency, obj.date)
        super().save_model(request, obj, form, change)


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
class ContractorTransactionAdmin(_FrozenRateAdminMixin, SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["contractor", "kind", "amount", "currency", "date", "production_run"]
    list_filter = ["kind", "currency"]
    search_fields = ["contractor__name", "note"]
    date_hierarchy = "date"
    autocomplete_fields = ["contractor"]
    # Set only by apps.manufacturing.services.record_production_run — never
    # hand-picked, same discipline as OrderItem.produced_qty.
    # rate_to_kgs/amount_kgs_display: see _FrozenRateAdminMixin.
    readonly_fields = ["production_run", *_FrozenRateAdminMixin.readonly_fields_extra]


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
class ExpenseAdmin(_FrozenRateAdminMixin, SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["date", "category", "amount", "currency", "contractor", "variant"]
    list_filter = ["category", "currency"]
    search_fields = ["note", "contractor__name", "variant__sku"]
    date_hierarchy = "date"
    autocomplete_fields = ["contractor", "variant"]
    # Set only by record_production_run — see ContractorTransaction.production_run.
    # rate_to_kgs/amount_kgs_display: see _FrozenRateAdminMixin.
    readonly_fields = ["production_run", *_FrozenRateAdminMixin.readonly_fields_extra]
