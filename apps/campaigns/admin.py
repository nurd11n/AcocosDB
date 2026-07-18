"""Compose a campaign, preview who it will reach, and send it — all from the
admin. Owner-only: superuser gates every action, and the app is absent from
Editor/Viewer sidebars (no permissions granted in setup_roles). Sending runs the
same send_campaign logic in-process; the audience count shown is the exact set
that will be messaged (shared campaign_audience helper)."""

from django.contrib import admin, messages
from django.core.management import call_command
from django.utils.translation import gettext_lazy as _

from .models import Campaign, CampaignRecipient
from .services import campaign_preview_count


class CampaignRecipientInline(admin.TabularInline):
    model = CampaignRecipient
    extra = 0
    can_delete = False
    fields = ["client", "status", "error", "sent_at"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ["name", "channel", "status", "reachable", "created_at"]
    list_filter = ["status", "channel"]
    filter_horizontal = ["products"]
    inlines = [CampaignRecipientInline]
    actions = ["send_now"]
    readonly_fields = ["status", "created_by", "created_at"]
    fieldsets = [
        (None, {"fields": ["name", "text_ru", "products", "channel"]}),
        (
            _("Audience"),
            {
                "fields": ["only_bought_before", "only_with_debt", "lapsed_days"],
                "description": _(
                    "Consent and a linked Telegram chat are always required on top "
                    "of these filters. Save, then use “Send” to preview the count."
                ),
            },
        ),
        (_("Status"), {"fields": ["status", "created_by", "created_at"]}),
    ]

    @admin.display(description=_("reachable"))
    def reachable(self, obj):
        return campaign_preview_count(obj)

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

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Send now (Telegram)"))
    def send_now(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, _("Only the owner can send campaigns."), messages.ERROR)
            return
        for campaign in queryset:
            count = campaign_preview_count(campaign)
            if count == 0:
                self.message_user(
                    request,
                    _("«%(name)s»: no reachable clients — nothing sent.") % {"name": campaign.name},
                    messages.WARNING,
                )
                continue
            try:
                call_command("send_campaign", campaign.pk)
            except Exception as exc:  # surface any send error in the admin, don't 500
                self.message_user(
                    request,
                    _("«%(name)s» failed: %(err)s") % {"name": campaign.name, "err": exc},
                    messages.ERROR,
                )
                continue
            self.message_user(
                request,
                _("«%(name)s» sent to %(n)s client(s).") % {"name": campaign.name, "n": count},
                messages.SUCCESS,
            )
