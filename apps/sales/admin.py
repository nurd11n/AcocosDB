from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin
from unfold.admin import ModelAdmin, TabularInline

from .models import Payment, SaleItem, SaleOrder
from .services import cancel_sale, confirm_sale


class SaleItemInline(TabularInline):
    model = SaleItem
    extra = 0
    autocomplete_fields = ["variant"]


@admin.register(SaleOrder)
class SaleOrderAdmin(SimpleHistoryAdmin, ModelAdmin):
    list_display = ["id", "client", "channel", "status", "total", "created_at", "confirmed_at"]
    list_filter = ["status", "channel"]
    search_fields = ["id", "client__name", "client__phone"]
    list_select_related = ["client"]
    autocomplete_fields = ["client"]
    readonly_fields = ["total", "status", "confirmed_at", "created_by"]
    inlines = [SaleItemInline]
    actions = ["confirm_selected", "cancel_selected"]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Confirm selected sales (writes off stock)"))
    def confirm_selected(self, request, queryset):
        for order in queryset:
            try:
                confirm_sale(order, user=request.user)
                self.message_user(request, _("Sale #%(id)s confirmed.") % {"id": order.pk})
            except ValidationError as exc:
                self.message_user(request, "; ".join(exc.messages), level=messages.ERROR)

    @admin.action(description=_("Cancel selected sales (returns stock)"))
    def cancel_selected(self, request, queryset):
        for order in queryset:
            try:
                cancel_sale(order, user=request.user)
                self.message_user(request, _("Sale #%(id)s cancelled.") % {"id": order.pk})
            except ValidationError as exc:
                self.message_user(request, "; ".join(exc.messages), level=messages.ERROR)


@admin.register(Payment)
class PaymentAdmin(SimpleHistoryAdmin, ModelAdmin):
    list_display = ["created_at", "client", "amount", "method", "order", "created_by"]
    list_filter = ["method"]
    search_fields = ["client__name", "client__phone"]
    list_select_related = ["client", "order", "created_by"]
    autocomplete_fields = ["client", "order"]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
