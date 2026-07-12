from django.db import models
from django.utils.translation import gettext_lazy as _


class BotUser(models.Model):
    """Allowlist for the Telegram bot. Unknown Telegram IDs are silently ignored."""

    telegram_id = models.BigIntegerField(_("Telegram ID"), unique=True)
    name = models.CharField(_("name"), max_length=120)
    can_see_costs = models.BooleanField(_("can see cost prices"), default=False)
    receives_reports = models.BooleanField(_("receives daily reports"), default=False)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("bot user")
        verbose_name_plural = _("bot users")

    def __str__(self):
        return f"{self.name} ({self.telegram_id})"


class BotMessage(models.Model):
    """Read-only log of every incoming/outgoing bot message, Telegram and WhatsApp
    alike, in one place — so all bot traffic is visible in the panel as a unit."""

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    CHANNEL_CHOICES = [(TELEGRAM, _("Telegram")), (WHATSAPP, _("WhatsApp"))]

    IN = "in"
    OUT = "out"
    DIRECTION_CHOICES = [(IN, _("Incoming")), (OUT, _("Outgoing"))]

    channel = models.CharField(_("channel"), max_length=16, choices=CHANNEL_CHOICES)
    external_id = models.CharField(
        _("external ID"),
        max_length=64,
        help_text=_("Telegram user ID or WhatsApp phone number"),
    )
    direction = models.CharField(_("direction"), max_length=3, choices=DIRECTION_CHOICES)
    text = models.TextField(_("text"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("bot message")
        verbose_name_plural = _("bot messages")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["channel", "external_id"])]

    def __str__(self):
        return f"{self.channel}/{self.direction} {self.external_id}: {self.text[:40]}"
