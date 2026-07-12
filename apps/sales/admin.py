from decimal import Decimal

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from .models import Payment, SaleItem, SaleOrder
from .services import cancel_sale, confirm_sale

_BADGE_COLORS = {
    SaleOrder.PAID: "#1a7f37",
    SaleOrder.PARTIAL: "#9a6700",
    SaleOrder.UNPAID: "#b42318",
}
_BADGE_LABELS = {
    SaleOrder.PAID: _("Paid"),
    SaleOrder.PARTIAL: _("Partial"),
    SaleOrder.UNPAID: _("Unpaid"),
}


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    autocomplete_fields = ["variant"]


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ["client", "amount", "method", "note", "created_by", "created_at"]
    readonly_fields = ["created_by", "created_at"]
    autocomplete_fields = ["client"]
    verbose_name_plural = _("payments (for loans / partial payment)")


@admin.register(SaleOrder)
class SaleOrderAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = [
        "id",
        "client",
        "channel",
        "status",
        "total",
        "paid_column",
        "balance_column",
        "payment_badge",
        "created_at",
        "confirmed_at",
    ]
    list_filter = ["status", "channel"]
    search_fields = ["id", "client__name", "client__phone"]
    list_select_related = ["client"]
    autocomplete_fields = ["client"]
    readonly_fields = ["total", "status", "confirmed_at", "created_by"]
    inlines = [SaleItemInline, PaymentInline]
    actions = ["confirm_selected", "cancel_selected"]

    def get_queryset(self, request):
        # Sum payments per order in SQL (one aggregate, joined to a stored total —
        # no row multiplication) so the payment badge stays single-query.
        return (
            super()
            .get_queryset(request)
            .annotate(
                _paid=Coalesce(
                    Sum("payments__amount"),
                    Value(Decimal("0")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                )
            )
        )

    @admin.display(description=_("paid"), ordering="_paid")
    def paid_column(self, obj):
        return obj._paid

    @admin.display(description=_("balance"))
    def balance_column(self, obj):
        if obj.status != SaleOrder.CONFIRMED:
            return "—"
        return max(obj.total - obj._paid, Decimal("0"))

    @admin.display(description=_("payment"))
    def payment_badge(self, obj):
        if obj.status != SaleOrder.CONFIRMED:
            return "—"
        status = SaleOrder.payment_status_for(obj.total, obj._paid)
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-size:11px;white-space:nowrap">{}</span>',
            _BADGE_COLORS[status],
            _BADGE_LABELS[status],
        )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        # Stamp created_by on inline payments, and default their client to the
        # order's client so a partial payment/loan needs one less field.
        instances = formset.save(commit=False)
        for obj in instances:
            if isinstance(obj, Payment):
                if obj.client_id is None and form.instance.client_id:
                    obj.client = form.instance.client
                if obj.created_by_id is None:
                    obj.created_by = request.user
            obj.save()
        formset.save_m2m()
        for obj in formset.deleted_objects:
            obj.delete()

    @admin.action(description=_("Approve selected sales (writes off stock)"))
    def confirm_selected(self, request, queryset):
        for order in queryset:
            try:
                confirm_sale(order, user=request.user)
                self.message_user(request, _("Sale #%(id)s approved.") % {"id": order.pk})
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
class PaymentAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["created_at", "client", "amount", "method", "order", "created_by"]
    list_filter = ["method"]
    search_fields = ["client__name", "client__phone"]
    list_select_related = ["client", "order", "created_by"]
    autocomplete_fields = ["client", "order"]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
