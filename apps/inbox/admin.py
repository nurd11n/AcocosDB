from django.contrib import admin

from .models import BotContent, Favourite, OrderRequest, OrderRequestItem, StockAlert


class OrderRequestItemInline(admin.TabularInline):
    model = OrderRequestItem
    extra = 0


@admin.register(OrderRequest)
class OrderRequestAdmin(admin.ModelAdmin):
    list_display = ["id", "client", "status", "source", "created_at", "handled_by"]
    list_filter = ["status", "source"]
    readonly_fields = ["handled_by", "handled_at", "order", "raw_message"]
    inlines = [OrderRequestItemInline]

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(Favourite)
class FavouriteAdmin(admin.ModelAdmin):
    list_display = ["client", "variant", "created_at"]

    def has_add_permission(self, request):
        return False  # created by clients via the bots, not typed by staff


@admin.register(StockAlert)
class StockAlertAdmin(admin.ModelAdmin):
    list_display = ["client", "variant", "created_at", "notified_at"]
    list_filter = ["notified_at"]

    def has_add_permission(self, request):
        return False


@admin.register(BotContent)
class BotContentAdmin(admin.ModelAdmin):
    """О нас / размеры / доставка — Owner-only, like every other piece of
    system-wide bot copy (see CampaignAdmin for the same pattern)."""

    list_display = ["key", "title_ru", "is_active"]
    prepopulated_fields = {"key": ["title_ru"]}

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
