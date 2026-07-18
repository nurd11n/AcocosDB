from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
    verbose_name = _("Inventory")

    def ready(self):
        from . import signals  # noqa: F401  (register cache-invalidation signals)
