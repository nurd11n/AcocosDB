from django.contrib import admin, messages
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import DailyReview


@admin.register(DailyReview)
class DailyReviewAdmin(admin.ModelAdmin):
    """Day-end review: today's payments, oldest first. Reviewing is Owner
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
    search_fields = ["client__name", "client__phone"]
    list_select_related = ["client", "order", "reviewed_by"]
    actions = ["mark_reviewed"]

    def get_queryset(self, request):
        today = timezone.localdate()
        return super().get_queryset(request).filter(created_at__date=today).order_by("created_at")

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
