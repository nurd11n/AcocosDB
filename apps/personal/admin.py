from django.conf import settings
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from apps.core.currency import format_money, snapshot_rate_to_base

from .models import PersonalExpense


def _owner_only_permissions(cls):
    """Same pattern as apps.manufacturing.admin._owner_only_permissions —
    duplicated rather than imported, since these two apps must stay able to
    change independently with zero coupling between them (the whole point of
    apps.personal being isolated)."""

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
@admin.register(PersonalExpense)
class PersonalExpenseAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["date", "tag", "amount", "currency", "description"]
    list_filter = ["tag", "currency"]
    search_fields = ["description"]
    date_hierarchy = "date"
    # rate_to_kgs/amount_kgs_display: see apps.manufacturing.admin.
    # _FrozenRateAdminMixin's own docstring — same defect class, same fix.
    # A hand-typed rate on this Owner-only form is exactly what filed
    # 181 000 сом as 15 928 000 in the manufacturing expense ledger.
    readonly_fields = ["amount_kgs_display", "rate_to_kgs"]

    @admin.display(description=_("in KGS (computed)"))
    def amount_kgs_display(self, obj):
        if obj is None or obj.pk is None:
            return _("— computed on save from the currency and date chosen")
        return format_money(obj.amount_kgs, settings.CURRENCY)

    def save_model(self, request, obj, form, change):
        # Freeze ONCE, on creation only — an edit keeps the original rate,
        # same reasoning as apps.manufacturing.admin's mixin.
        if not change:
            obj.rate_to_kgs = snapshot_rate_to_base(obj.currency, obj.date)
        super().save_model(request, obj, form, change)
