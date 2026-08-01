from django.contrib import admin, messages
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import DailyReview


@admin.register(DailyReview)
class DailyReviewAdmin(admin.ModelAdmin):
    """The payment-review queue: newest first, defaulting to the ones still
    awaiting a look (reviewed=No) so nothing is ever silently missed — a
    payment made yesterday and not yet reviewed still shows today. Filter by
    the реviewed pill or drill by date to see history. Reviewing is Owner
    (superuser) only; Editor/Viewer can see the list but not act on it."""

    list_display = [
        "created_at",
        "client",
        "order",
        "amount",
        "currency",
        "method",
        "reviewed",
        "reviewed_by",
    ]
    list_filter = ["reviewed", "method", "currency"]
    date_hierarchy = "created_at"
    search_fields = ["client__first_name", "client__descriptor", "client__phone"]
    search_help_text = "Имя клиента, уточнение или телефон"
    list_select_related = ["client", "order", "reviewed_by"]
    actions = ["mark_reviewed"]

    # Unreviewed first (the queue that needs attention), then newest. No date
    # restriction — the old "today only" filter (a) hid every payment on any day
    # nothing was sold and (b) matched on the UTC date, not the shop's, so
    # late-evening payments fell off. The review queue must never lose an
    # unreviewed payment. Filter by the «Проверено» pill to hide done ones.
    ordering = ["reviewed", "-created_at"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        # Review happens via the bulk action, not by editing fields on a form.
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("mark_reviewed", None)
        return actions

    @admin.action(description=_("Mark reviewed (Отметить проверенным)"))
    def mark_reviewed(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(
                request,
                _("Only a superuser can mark payments as reviewed."),
                level=messages.ERROR,
            )
            return
        updated = queryset.filter(reviewed=False).update(
            reviewed=True, reviewed_by=request.user, reviewed_at=timezone.now()
        )
        self.message_user(request, _("Marked %(n)s payment(s) reviewed.") % {"n": updated})
