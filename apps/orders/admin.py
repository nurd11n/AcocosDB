from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from apps.core.admin_confirm import confirm_bulk_action
from apps.core.currency import format_money

from .models import Order, OrderItem
from .services import cancel_order, order_paid_amount


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    autocomplete_fields = ["variant"]
    readonly_fields = ["produced_qty"]  # written only by services.mark_produced (POS)


@admin.register(Order)
class OrderAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = [
        "id",
        "client",
        "status",
        "due_date",
        "overdue_display",
        "total_display",
        "paid_display",
        "produced_summary",
        "created_at",
    ]
    list_filter = ["status", "currency"]
    search_fields = ["id", "client__first_name", "client__descriptor", "client__phone"]
    search_help_text = "Номер заказа, имя клиента, уточнение или телефон"
    date_hierarchy = "created_at"
    list_select_related = ["client"]
    autocomplete_fields = ["client"]
    readonly_fields = ["sale_order", "delivered_at", "created_by"]
    inlines = [OrderItemInline]
    actions = ["cancel_selected"]

    @admin.display(description=_("total"))
    def total_display(self, obj):
        return format_money(obj.total, obj.currency)

    @admin.display(description=_("paid"))
    def paid_display(self, obj):
        return format_money(order_paid_amount(obj), obj.currency)

    @admin.display(description=_("overdue"), boolean=True)
    def overdue_display(self, obj):
        return obj.is_overdue

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        # Owner-only, per CLAUDE.md Part 3h.
        return request.user.is_superuser

    @admin.action(description=_("Cancel selected orders (releases reservation)"))
    def cancel_selected(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(
                request, _("Cancelling an order is Owner-only."), level=messages.ERROR
            )
            return None
        resp = confirm_bulk_action(
            request,
            queryset,
            action_name="cancel_selected",
            title=_("Cancel orders"),
            warning=_(
                "Each order's reservation is released immediately. This cannot "
                "be undone from here."
            ),
            admin_site=self.admin_site,
            model_opts=self.model._meta,
        )
        if resp is not None:
            return resp
        cancelled = 0
        for order in queryset:
            try:
                cancel_order(order, user=request.user)
                cancelled += 1
            except ValidationError as exc:
                self.message_user(
                    request, f"#{order.pk}: " + "; ".join(exc.messages), level=messages.ERROR
                )
        self.message_user(request, _("Cancelled %(n)s order(s).") % {"n": cancelled})
        return None
