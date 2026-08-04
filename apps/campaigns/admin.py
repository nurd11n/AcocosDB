"""Compose a campaign, preview who it will reach, and send it — all from the
admin. Owner-only: superuser gates every action, and the app is absent from
Editor/Viewer sidebars (no permissions granted in setup_roles). Sending runs the
same send_campaign logic in-process; the audience count shown is the exact set
that will be messaged (shared campaign_audience helper).

CAMPAIGNS_ENABLED=False (the shipped prod default — bots aren't
production-ready yet) hides this module from the sidebar for EVERYONE,
including the superuser, and every has_*_permission check below denies —
not just hidden, actually unreachable even by direct URL."""

import logging

from django.conf import settings
from django.contrib import admin, messages
from django.core.management import call_command
from django.utils.translation import gettext_lazy as _

from apps.core.admin_confirm import confirm_bulk_action

from .models import Campaign, CampaignRecipient
from .services import campaign_preview_count

logger = logging.getLogger(__name__)


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
                "fields": [
                    "only_bought_before",
                    "only_with_debt",
                    "lapsed_days",
                    "only_favourited_product",
                ],
                "description": _(
                    "Consent and a reachable channel (Telegram chat / WhatsApp opt-in) "
                    "are always required on top of these filters, and the 2-per-week "
                    "frequency cap always applies. Save, then use “Send” to preview the count."
                ),
            },
        ),
        (_("Status"), {"fields": ["status", "created_by", "created_at"]}),
    ]

    @admin.display(description=_("reachable"))
    def reachable(self, obj):
        return campaign_preview_count(obj)

    def has_module_permission(self, request):
        return settings.CAMPAIGNS_ENABLED and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return settings.CAMPAIGNS_ENABLED and request.user.is_superuser

    def has_add_permission(self, request):
        return settings.CAMPAIGNS_ENABLED and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return settings.CAMPAIGNS_ENABLED and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return settings.CAMPAIGNS_ENABLED and request.user.is_superuser

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Send now (Telegram)"))
    def send_now(self, request, queryset):
        if not settings.CAMPAIGNS_ENABLED:
            self.message_user(request, _("Campaigns are disabled."), messages.ERROR)
            return None
        if not request.user.is_superuser:
            self.message_user(request, _("Only the owner can send campaigns."), messages.ERROR)
            return None
        resp = confirm_bulk_action(
            request,
            queryset,
            action_name="send_now",
            title=_("Send campaigns"),
            warning=_(
                "Each campaign below goes out immediately to its full reachable "
                "audience. This cannot be undone or recalled once sent."
            ),
            admin_site=self.admin_site,
            model_opts=self.model._meta,
        )
        if resp is not None:
            return resp
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
            except Exception:
                # The raw exception text is a Python-internals leak to a
                # non-technical Owner, with no next step — log it for a
                # developer to actually diagnose, show a plain Russian
                # message that says what to do instead.
                logger.exception("send_campaign failed for campaign #%s", campaign.pk)
                self.message_user(
                    request,
                    _(
                        "«%(name)s»: отправка не удалась. Попробуйте ещё раз позже; "
                        "если повторится — обратитесь к разработчику."
                    )
                    % {"name": campaign.name},
                    messages.ERROR,
                )
                continue
            self.message_user(
                request,
                _("«%(name)s» sent to %(n)s client(s).") % {"name": campaign.name, "n": count},
                messages.SUCCESS,
            )
        return None
