from django.utils.translation import gettext_lazy as _

from apps.sales.models import Payment


class DailyReview(Payment):
    """Proxy of Payment, grouped under its own 'Отчёты' sidebar section — the
    day-end review changelist of today's payments awaiting a look."""

    class Meta:
        proxy = True
        verbose_name = _("payment review")
        verbose_name_plural = _("payment review")
