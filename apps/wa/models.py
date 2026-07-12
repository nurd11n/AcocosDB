from django.db import models
from django.utils.translation import gettext_lazy as _


class WhatsAppMessage(models.Model):
    IN = "in"
    OUT = "out"
    DIRECTION_CHOICES = [(IN, _("Incoming")), (OUT, _("Outgoing"))]

    wa_id = models.CharField(_("WhatsApp ID / phone"), max_length=32)
    direction = models.CharField(_("direction"), max_length=3, choices=DIRECTION_CHOICES)
    text = models.TextField(_("text"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("WhatsApp message")
        verbose_name_plural = _("WhatsApp messages")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.direction} {self.wa_id}: {self.text[:40]}"
