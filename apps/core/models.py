from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class ExchangeRate(models.Model):
    """Manual FX rate for display-only conversion: on `date`, 1 unit of
    `currency` equals `rate` units of the base currency (settings.CURRENCY).
    Superuser-only (lives under Система) — stored sale/payment amounts are
    never auto-converted; this only powers a converted figure for display."""

    MANUAL = "manual"
    NBKR = "nbkr"
    SOURCE_CHOICES = [(MANUAL, _("Manual")), (NBKR, _("NBKR"))]

    currency = models.CharField(_("currency"), max_length=3)
    date = models.DateField(_("date"))
    rate = models.DecimalField(
        _("rate"),
        max_digits=14,
        decimal_places=6,
        help_text=_("How many units of the base currency equal 1 unit of this currency."),
    )
    # Where the rate came from: NBKR auto-fetch or an owner's manual entry. The
    # fetch_rates command never clobbers a manual row (owner override wins).
    source = models.CharField(_("source"), max_length=16, choices=SOURCE_CHOICES, default=MANUAL)

    class Meta:
        verbose_name = _("exchange rate")
        verbose_name_plural = _("exchange rates")
        ordering = ["-date", "currency"]
        constraints = [
            models.UniqueConstraint(fields=["currency", "date"], name="uniq_rate_per_currency_date")
        ]

    def __str__(self):
        return f"{self.date}: 1 {self.currency} = {self.rate} {settings.CURRENCY}"

    def clean(self):
        from .currency import CURRENCY_CODES

        if self.currency == settings.CURRENCY:
            raise ValidationError(
                _("The base currency (%(base)s) is always 1 — no rate needed.")
                % {"base": settings.CURRENCY}
            )
        if self.currency not in CURRENCY_CODES:
            raise ValidationError(
                _("Unknown currency. Configured currencies: %(list)s.")
                % {"list": ", ".join(CURRENCY_CODES)}
            )
        if self.rate is not None and self.rate <= 0:
            raise ValidationError(_("Rate must be greater than zero."))


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
