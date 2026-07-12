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

    name = models.CharField(_("name"), max_length=200)
    phone = models.CharField(_("phone"), max_length=32, unique=True)
    source = models.CharField(_("source"), max_length=16, choices=SOURCE_CHOICES, blank=True)
    note = models.TextField(_("note"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("client")
        verbose_name_plural = _("clients")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.phone})"


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
