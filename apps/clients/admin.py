from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from .models import Client, Interaction
from .services import clients_with_debt


class InteractionInline(admin.TabularInline):
    model = Interaction
    extra = 0
    fields = ["kind", "note", "created_by", "created_at"]
    readonly_fields = ["created_by", "created_at"]


@admin.register(Client)
class ClientAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["name", "phone", "source", "debt", "is_active", "created_at"]
    search_fields = ["name", "phone"]
    list_filter = ["source", "is_active"]
    inlines = [InteractionInline]

    def get_queryset(self, request):
        # Debt for the whole page in one query (two correlated subqueries).
        return clients_with_debt()

    @admin.display(description=_("debt"), ordering="sales_total")
    def debt(self, obj):
        return obj.sales_total - obj.payments_total


@admin.register(Interaction)
class InteractionAdmin(admin.ModelAdmin):
    list_display = ["created_at", "client", "kind", "note", "created_by"]
    list_filter = ["kind"]
    search_fields = ["client__name", "client__phone", "note"]
    list_select_related = ["client", "created_by"]
    autocomplete_fields = ["client"]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
