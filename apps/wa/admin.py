from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import WhatsAppMessage


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(ModelAdmin):
    list_display = ["created_at", "direction", "wa_id", "text"]
    list_filter = ["direction"]
    search_fields = ["wa_id", "text"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
