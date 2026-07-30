from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from .models import Client, Interaction
from .services import client_debts_by_currency


class InteractionInline(admin.TabularInline):
    model = Interaction
    extra = 0
    fields = ["kind", "note", "created_by", "created_at"]
    readonly_fields = ["created_by", "created_at"]


@admin.register(Client)
class ClientAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["first_name", "last_name", "phone", "source", "debt", "is_active", "created_at"]
    search_fields = ["first_name", "last_name", "phone"]
    list_filter = ["source", "is_active"]
    readonly_fields = ["unpaid_orders"]
    inlines = [InteractionInline]

    def get_queryset(self, request):
        # Debt is per-currency now, so it can't be a single annotated column —
        # one extra pair of grouped queries for the whole page (no N+1), cached
        # on the ModelAdmin instance for the duration of this request/response.
        self._debts = client_debts_by_currency()
        return super().get_queryset(request)

    @admin.display(description=_("debt"))
    def debt(self, obj):
        debts = {cur: amt for cur, amt in self._debts.get(obj.pk, {}).items() if amt > 0}
        if not debts:
            return "—"
        return ", ".join(f"{amt} {cur}" for cur, amt in sorted(debts.items()))

    @admin.display(description=_("unpaid orders"))
    def unpaid_orders(self, obj):
        """Approved orders this client still owes on — quick navigation from the
        client straight to what's outstanding."""
        if not obj.pk:
            return "—"
        from apps.sales.models import SaleOrder

        orders = SaleOrder.objects.filter(client=obj, status=SaleOrder.CONFIRMED).order_by(
            "-confirmed_at"
        )
        # o.balance already converts every payment into the order's own
        # currency at its frozen rate (see SaleOrder.paid_amount) and applies
        # the rounding tolerance — a same-currency-only sum would wrongly
        # list an order as unpaid when it was settled by a foreign payment.
        rows = [(o, o.balance) for o in orders if o.balance > 0]
        if not rows:
            return _("None — all settled.")
        body = format_html_join(
            "",
            '<tr><td style="padding:2px 10px 2px 0"><a href="{}">#{}</a></td>'
            '<td style="padding:2px 10px;text-align:right">{} {}</td></tr>',
            (
                (reverse("admin:sales_saleorder_change", args=[o.pk]), o.pk, balance, o.currency)
                for o, balance in rows
            ),
        )
        return format_html("<table>{}</table>", body)


@admin.register(Interaction)
class InteractionAdmin(admin.ModelAdmin):
    list_display = ["created_at", "client", "kind", "note", "created_by"]
    list_filter = ["kind"]
    search_fields = ["client__first_name", "client__last_name", "client__phone", "note"]
    list_select_related = ["client", "created_by"]
    autocomplete_fields = ["client"]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
