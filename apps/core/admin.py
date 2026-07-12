from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import BotUser


@admin.register(BotUser)
class BotUserAdmin(ModelAdmin):
    list_display = ["name", "telegram_id", "can_see_costs", "is_active", "created_at"]
    list_filter = ["is_active", "can_see_costs"]
    search_fields = ["name", "telegram_id"]
