from urllib.parse import quote

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from apps.core.currency import format_money

from .models import Client, ClientOpeningBalance, Interaction
from .services import client_debts_by_currency


def _xlsx_response(content: bytes, filename: str) -> HttpResponse:
    """Content-Disposition with a real UTF-8 filename (client names are
    Cyrillic almost always) — the plain filename="" parameter must stay pure
    ASCII or some clients mishandle/reject the header entirely, so this sends
    BOTH: an ASCII-safe fallback and the real name via the standard
    filename*=UTF-8'' extension (RFC 5987/6266) every modern browser honours."""
    ascii_fallback = "".join(ch if ch.isascii() else "_" for ch in filename) or "export.xlsx"
    resp = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = (
        f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
    )
    return resp


class InteractionInline(admin.TabularInline):
    model = Interaction
    extra = 0
    fields = ["kind", "note", "created_by", "created_at"]
    readonly_fields = ["created_by", "created_at"]


class HasDebtFilter(admin.SimpleListFilter):
    """«Кто должен» in one click. Debt is derived (confirmed sales minus
    payments, each converted at its own frozen rate — see
    clients.services.client_debts_by_currency), so it can't be a plain
    field filter; this resolves the same helper to a client-id set and
    filters on that."""

    title = _("debt")
    parameter_name = "has_debt"

    def lookups(self, request, model_admin):
        return [("yes", _("Owes money")), ("no", _("Nothing owed"))]

    def queryset(self, request, queryset):
        if self.value() not in ("yes", "no"):
            return queryset
        debtors = [
            cid
            for cid, currs in client_debts_by_currency().items()
            if any(amount > 0 for amount in currs.values())
        ]
        return (
            queryset.filter(pk__in=debtors)
            if self.value() == "yes"
            else queryset.exclude(pk__in=debtors)
        )


@admin.register(Client)
class ClientAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = [
        "first_name",
        "descriptor",
        "phone",
        "source",
        "debt",
        "is_active",
        "created_at",
    ]
    search_fields = ["first_name", "descriptor", "phone"]
    search_help_text = "Имя, уточнение или телефон"
    list_filter = [HasDebtFilter, "source", "is_active"]
    readonly_fields = ["unpaid_orders", "export_link"]
    inlines = [InteractionInline]
    actions = ["export_selected"]

    def get_queryset(self, request):
        # Debt is per-currency now, so it can't be a single annotated column —
        # one extra pair of grouped queries for the whole page (no N+1), cached
        # on the ModelAdmin instance for the duration of this request/response.
        self._debts = client_debts_by_currency()
        return super().get_queryset(request)

    # --- Excel export: transaction history + per-currency debt summary.
    # Reachable by anyone who can already VIEW this client (Editor and
    # Viewer both hold clients.view_client — the export is just a different
    # rendering of data they already see on screen, not a new disclosure). ---

    def get_urls(self):
        custom = [
            path(
                "<int:pk>/export/",
                self.admin_site.admin_view(self.export_view),
                name="clients_client_export",
            ),
        ]
        return custom + super().get_urls()

    @admin.display(description=_("export"))
    def export_link(self, obj):
        if not obj.pk:
            return "—"
        url = reverse("admin:clients_client_export", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("Выгрузить в Excel"))

    def export_view(self, request, pk):
        client = self.get_object(request, pk)
        if client is None:
            raise Http404
        if not self.has_view_permission(request, client):
            raise PermissionDenied
        from apps.reports.client_export import client_export_filename, client_workbook_bytes

        content = client_workbook_bytes([client])
        return _xlsx_response(content, client_export_filename(client))

    @admin.action(description=_("Export to Excel (Выгрузить в Excel)"))
    def export_selected(self, request, queryset):
        from apps.reports.client_export import client_workbook_bytes

        content = client_workbook_bytes(queryset)
        filename = f"clients_export_{timezone.localdate():%Y-%m-%d}.xlsx"
        return _xlsx_response(content, filename)

    @admin.display(description=_("debt"))
    def debt(self, obj):
        debts = {cur: amt for cur, amt in self._debts.get(obj.pk, {}).items() if amt > 0}
        if not debts:
            return "—"
        return ", ".join(format_money(amt, cur) for cur, amt in sorted(debts.items()))

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
            '<td style="padding:2px 10px;text-align:right">{}</td></tr>',
            (
                (
                    reverse("admin:sales_saleorder_change", args=[o.pk]),
                    o.pk,
                    format_money(balance, o.currency),
                )
                for o, balance in rows
            ),
        )
        return format_html("<table>{}</table>", body)


@admin.register(Interaction)
class InteractionAdmin(admin.ModelAdmin):
    list_display = ["created_at", "client", "kind", "note", "created_by"]
    list_filter = ["kind"]
    search_fields = ["client__first_name", "client__descriptor", "client__phone", "note"]
    search_help_text = "Имя клиента, телефон или текст записи"
    list_select_related = ["client", "created_by"]
    autocomplete_fields = ["client"]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(ClientOpeningBalance)
class ClientOpeningBalanceAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    """Pre-system debt — a money-affecting control, same tier as
    ExchangeRate's manual override: Owner-only to view, add, change, or
    delete, enforced with explicit is_superuser checks below (never just
    hidden from Editor/Viewer's Django permissions, the same defence-in-
    depth ExchangeRateAdmin already uses for the identical reason)."""

    list_display = ["client", "amount", "currency", "as_of_date", "note", "created_by"]
    list_filter = ["currency"]
    search_fields = ["client__first_name", "client__descriptor", "client__phone"]
    date_hierarchy = "as_of_date"
    autocomplete_fields = ["client"]

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

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def changelist_view(self, request, extra_context=None):
        # «Старый долг внесён для N из M клиентов» — a migration-progress
        # readout so she can tell how much of the by-hand entry is done
        # without cross-checking the client list herself. N = distinct
        # clients with at least one opening balance row (any currency), not
        # row count — a client with both a KGS and a USD opening balance is
        # still ONE client "done".
        extra_context = extra_context or {}
        total_clients = Client.objects.filter(is_active=True).count()
        covered_clients = ClientOpeningBalance.objects.values("client_id").distinct().count()
        extra_context["opening_balance_progress"] = _(
            "Старый долг внесён для %(n)s из %(m)s клиентов"
        ) % {"n": covered_clients, "m": total_clients}
        return super().changelist_view(request, extra_context=extra_context)
