"""Marketing broadcasts. Owner-only (kept out of BUSINESS_APPS, so it never
appears for Editor/Viewer). A Campaign is composed once, then sent by the
`send_campaign` management command, which records one CampaignRecipient per
client so a send is auditable and never repeated. Telegram is the only channel
implemented; WhatsApp is modelled but feature-gated off.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Campaign(models.Model):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    CHANNEL_CHOICES = [(TELEGRAM, _("Telegram")), (WHATSAPP, _("WhatsApp"))]

    DRAFT = "draft"
    SENDING = "sending"
    SENT = "sent"
    STATUS_CHOICES = [(DRAFT, _("Draft")), (SENDING, _("Sending")), (SENT, _("Sent"))]

    name = models.CharField(_("name"), max_length=120)
    text_ru = models.TextField(_("message (Russian)"))
    products = models.ManyToManyField(
        "inventory.Product",
        verbose_name=_("products"),
        blank=True,
        help_text=_("Their photos are sent as a media group with the message."),
    )
    channel = models.CharField(
        _("channel"), max_length=16, choices=CHANNEL_CHOICES, default=TELEGRAM
    )
    status = models.CharField(_("status"), max_length=16, choices=STATUS_CHOICES, default=DRAFT)

    # Audience filters (consent + a reachable Telegram chat are ALWAYS required
    # on top of these — enforced in services.campaign_audience, not optional).
    only_bought_before = models.BooleanField(_("only clients who bought before"), default=False)
    only_with_debt = models.BooleanField(_("only clients with debt"), default=False)
    lapsed_days = models.PositiveIntegerField(
        _("only clients inactive for N days"),
        null=True,
        blank=True,
        help_text=_("Leave empty to ignore. E.g. 60 = haven't purchased in 60 days."),
    )
    # The highest-converting segment by far (CLIENT_BOTS.md §5.1): clients who
    # favourited a variant of THIS product on a client bot.
    only_favourited_product = models.ForeignKey(
        "inventory.Product",
        verbose_name=_("only clients who favourited this product"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("campaign")
        verbose_name_plural = _("campaigns")
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class CampaignRecipient(models.Model):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
    STATUS_CHOICES = [
        (PENDING, _("Pending")),
        (SENT, _("Sent")),
        (FAILED, _("Failed")),
        (SKIPPED, _("Skipped")),
    ]

    campaign = models.ForeignKey(
        Campaign, verbose_name=_("campaign"), on_delete=models.CASCADE, related_name="recipients"
    )
    client = models.ForeignKey("clients.Client", verbose_name=_("client"), on_delete=models.CASCADE)
    status = models.CharField(_("status"), max_length=16, choices=STATUS_CHOICES, default=PENDING)
    error = models.CharField(_("error"), max_length=255, blank=True)
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)

    class Meta:
        verbose_name = _("campaign recipient")
        verbose_name_plural = _("campaign recipients")
        # One row per client per campaign — the guarantee that nobody is
        # messaged twice, even if the send command is re-run after a crash.
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "client"], name="uniq_recipient_per_campaign"
            )
        ]

    def __str__(self):
        return f"{self.campaign} → {self.client} [{self.status}]"
