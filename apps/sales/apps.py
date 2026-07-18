from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SalesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sales"
    verbose_name = _("Sales")

    def ready(self):
        from . import signals  # noqa: F401  (dashboard cache invalidation on payments)
