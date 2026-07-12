from django.db import models
from django.utils.translation import gettext_lazy as _


class BotUser(models.Model):
    """Allowlist for the Telegram bot. Unknown Telegram IDs are silently ignored."""

    telegram_id = models.BigIntegerField(_("Telegram ID"), unique=True)
    name = models.CharField(_("name"), max_length=120)
    can_see_costs = models.BooleanField(_("can see cost prices"), default=False)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("bot user")
        verbose_name_plural = _("bot users")

    def __str__(self):
        return f"{self.name} ({self.telegram_id})"
