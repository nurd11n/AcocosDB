from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class ExchangeRate(models.Model):
    """The CURRENT FX rate per currency: 1 unit of `currency` = `rate` units of
    the base (settings.CURRENCY). ONE row per currency — refreshing (the POS
    «Обновить» button, or a manual admin edit) overwrites it in place, never
    piles up a new dated row. `date` records when it was last updated.
    Superuser-only (lives under Система)."""

    MANUAL = "manual"
    NBKR = "nbkr"
    FRANKFURTER = "frankfurter"
    # NBKR data now arrives via the Frankfurter API (nbkr.kg itself blocks
    # this server's IP), pinned to its NBKR provider — so a Frankfurter-
    # sourced rate is still genuinely NBKR data and stays labelled NBKR.
    # FRANKFURTER exists only for the (currently unused) case where the
    # pinned provider ever isn't NBKR — the label must never lie about that.
    SOURCE_CHOICES = [(MANUAL, _("Manual")), (NBKR, _("NBKR")), (FRANKFURTER, _("Frankfurter"))]

    currency = models.CharField(_("currency"), max_length=3, unique=True)
    date = models.DateField(_("updated"), help_text=_("When this rate was last updated."))
    rate = models.DecimalField(
        _("rate"),
        max_digits=14,
        decimal_places=6,
        help_text=_("How many units of the base currency equal 1 unit of this currency."),
    )
    # Where the rate came from: NBKR pull or an owner's manual entry.
    source = models.CharField(_("source"), max_length=16, choices=SOURCE_CHOICES, default=MANUAL)

    class Meta:
        verbose_name = _("exchange rate")
        verbose_name_plural = _("exchange rates")
        ordering = ["currency"]

    def __str__(self):
        return f"1 {self.currency} = {self.rate} {settings.CURRENCY}"

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


class RateChangeLog(models.Model):
    """Append-only audit trail: every ACTUAL change to an ExchangeRate (an NBKR
    refresh that moved the number, or an owner's manual edit) — who, when, old
    value, new value, source. Never written for a refresh that pulled the same
    rate back (no change happened). Read-only, Owner-only (see admin)."""

    currency = models.CharField(_("currency"), max_length=3)
    old_rate = models.DecimalField(
        _("old rate"), max_digits=14, decimal_places=6, null=True, blank=True
    )
    new_rate = models.DecimalField(_("new rate"), max_digits=14, decimal_places=6)
    source = models.CharField(_("source"), max_length=16, choices=ExchangeRate.SOURCE_CHOICES)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("changed by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_("Empty for the automatic scheduled/system refresh."),
    )
    changed_at = models.DateTimeField(_("changed at"), auto_now_add=True)

    class Meta:
        verbose_name = _("rate change")
        verbose_name_plural = _("rate change log")
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.currency}: {self.old_rate} → {self.new_rate} ({self.source})"


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
    # Nullable: staff-bot traffic (BotUser, not a Client) never sets this.
    # Set by the client/whatsapp send paths so /inbox/ can thread a client's
    # messages across both channels in one place (CLAUDE.md-style unified inbox).
    client = models.ForeignKey(
        "clients.Client",
        verbose_name=_("client"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bot_messages",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("bot message")
        verbose_name_plural = _("bot messages")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["channel", "external_id"]),
            models.Index(fields=["client", "created_at"]),
        ]

    def __str__(self):
        return f"{self.channel}/{self.direction} {self.external_id}: {self.text[:40]}"
