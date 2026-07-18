from django import forms
from django.conf import settings
from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .currency import CURRENCY_CODES
from .models import BotMessage, BotUser, ExchangeRate


class ExchangeRateForm(forms.ModelForm):
    class Meta:
        model = ExchangeRate
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        extra = [c for c in CURRENCY_CODES if c != settings.CURRENCY]
        self.fields["currency"] = forms.ChoiceField(
            choices=[(c, c) for c in extra], label=_("currency")
        )
        if not self.instance.pk:
            self.fields["date"].initial = timezone.localdate()


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    """Manual FX rates for the dashboard converter and report totals. 1 unit of
    `currency` = `rate` of the base currency on that date. Superuser-only."""

    form = ExchangeRateForm
    list_display = ["date", "currency", "one_unit"]
    list_filter = ["currency"]
    date_hierarchy = "date"

    @admin.display(description=_("rate"))
    def one_unit(self, obj):
        return f"1 {obj.currency} = {obj.rate.normalize():f} {settings.CURRENCY}"


@admin.register(BotUser)
class BotUserAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "telegram_id",
        "can_see_costs",
        "receives_reports",
        "is_active",
        "created_at",
    ]
    list_filter = ["is_active", "can_see_costs", "receives_reports"]
    search_fields = ["name", "telegram_id"]


@admin.register(BotMessage)
class BotMessageAdmin(admin.ModelAdmin):
    """All bot traffic (Telegram + WhatsApp) is read-only in the panel."""

    list_display = ["created_at", "channel", "direction", "external_id", "text"]
    list_filter = ["channel", "direction"]
    search_fields = ["external_id", "text"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
