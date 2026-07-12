from django.contrib import admin

from .models import BotMessage, BotUser


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
