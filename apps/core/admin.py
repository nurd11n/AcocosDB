from django import forms
from django.conf import settings
from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .currency import CURRENCY_CODES
from .models import BotMessage, BotUser, ExchangeRate, RateChangeLog


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

    def validate_unique(self):
        # Adding a rate for a currency that already has one OVERWRITES it (see
        # ExchangeRateAdmin.save_model), so don't block on unique(currency).
        pass


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    """The current FX rate per currency (1 unit of `currency` = `rate` of the
    base currency). One row per currency — adding or editing overwrites it in
    place; staff normally refresh from the POS «Курс» card. Hand-entering or
    overriding a rate here is a money-affecting control, so it's Owner-only —
    enforced with explicit permission checks below, not just structural
    exclusion from Editor/Viewer's Django permissions (BUSINESS_APPS)."""

    form = ExchangeRateForm
    list_display = ["currency", "one_unit", "date"]

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
        # Upsert by currency: adding a rate for a currency that already exists
        # overwrites that row rather than erroring on the unique constraint.
        existing = ExchangeRate.objects.filter(currency=obj.currency).exclude(pk=obj.pk).first()
        old_rate = existing.rate if existing else None
        if existing:
            obj.pk = existing.pk
        if not obj.date:
            obj.date = timezone.localdate()
        obj.source = ExchangeRate.MANUAL
        super().save_model(request, obj, form, change)
        if old_rate is None or old_rate != obj.rate:
            from .models import RateChangeLog

            RateChangeLog.objects.create(
                currency=obj.currency,
                old_rate=old_rate,
                new_rate=obj.rate,
                source=ExchangeRate.MANUAL,
                changed_by=request.user,
            )

    @admin.display(description=_("rate"))
    def one_unit(self, obj):
        return f"1 {obj.currency} = {obj.rate.normalize():f} {settings.CURRENCY}"


@admin.register(RateChangeLog)
class RateChangeLogAdmin(admin.ModelAdmin):
    """Append-only audit trail of every rate change — Owner-only, read-only."""

    list_display = ["changed_at", "currency", "old_rate", "new_rate", "source", "changed_by"]
    list_filter = ["currency", "source"]
    date_hierarchy = "changed_at"

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
