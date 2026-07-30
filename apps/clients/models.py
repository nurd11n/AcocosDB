from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords


class Client(models.Model):
    INSTAGRAM = "instagram"
    WHATSAPP = "whatsapp"
    SHOP = "shop"
    WHOLESALE = "wholesale"
    SOURCE_CHOICES = [
        (INSTAGRAM, "Instagram"),
        (WHATSAPP, "WhatsApp"),
        (SHOP, _("Shop")),
        (WHOLESALE, _("Wholesale")),
    ]

    first_name = models.CharField(_("first name"), max_length=100, default="")
    last_name = models.CharField(_("last name"), max_length=100, blank=True)
    phone = models.CharField(_("phone"), max_length=32, unique=True)
    source = models.CharField(_("source"), max_length=16, choices=SOURCE_CHOICES, blank=True)
    note = models.TextField(_("note"), blank=True)
    # Marketing reachability. telegram_chat_id is set only once a client presses
    # /start on the bot — the ONLY way Telegram lets us message them (there is no
    # send-by-phone). marketing_consent gates every broadcast and is cleared by
    # a «СТОП»/«STOP» reply. whatsapp_opted_in gates the (later) WhatsApp channel.
    telegram_chat_id = models.BigIntegerField(
        _("Telegram chat ID"), null=True, blank=True, unique=True
    )
    marketing_consent = models.BooleanField(_("marketing consent"), default=False)
    whatsapp_opted_in = models.BooleanField(_("WhatsApp opt-in"), default=False)
    RU = "ru"
    KY = "ky"
    BOT_LANGUAGE_CHOICES = [(RU, "Русский"), (KY, "Кыргызча")]
    bot_language = models.CharField(
        _("bot language"), max_length=4, choices=BOT_LANGUAGE_CHOICES, default=RU
    )
    # WhatsApp auto-reply goes quiet until this moment — set on «менеджер», on
    # any staff reply from /inbox/, or on 3+ client messages in 2 minutes (see
    # apps.wa.replies). NULL/past = bot answers normally.
    human_handoff_until = models.DateTimeField(_("human handoff until"), null=True, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("client")
        verbose_name_plural = _("clients")
        ordering = ["first_name", "last_name"]

    def __str__(self):
        return f"{self.name} ({self.phone})"

    @property
    def name(self) -> str:
        """Combined display name — kept as a read property so reports, bot
        replies, and the WhatsApp CRM-linking code didn't need to change when
        the field split into first_name/last_name."""
        return f"{self.first_name} {self.last_name}".strip()


class Interaction(models.Model):
    CALL = "call"
    MESSAGE = "message"
    VISIT = "visit"
    KIND_CHOICES = [(CALL, _("Call")), (MESSAGE, _("Message")), (VISIT, _("Visit"))]

    client = models.ForeignKey(
        Client, verbose_name=_("client"), on_delete=models.CASCADE, related_name="interactions"
    )
    kind = models.CharField(_("type"), max_length=16, choices=KIND_CHOICES)
    note = models.TextField(_("note"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("interaction")
        verbose_name_plural = _("interactions")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.client}"
