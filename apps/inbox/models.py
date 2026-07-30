from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class BotContent(models.Model):
    """Static client-facing copy («О нас»: working hours, address, size guide,
    care instructions, return policy) — editable from the panel, never
    hardcoded in bot/wa code, so the owner can fix a typo without a deploy."""

    key = models.SlugField(_("key"), max_length=64, unique=True)
    title_ru = models.CharField(_("title (Russian)"), max_length=200)
    body_ru = models.TextField(_("body (Russian)"))
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("bot content")
        verbose_name_plural = _("bot content")
        ordering = ["key"]

    def __str__(self):
        return self.title_ru


class OrderRequest(models.Model):
    """Заявка — an inbound lead from a client-facing bot. NEVER reserves
    stock, sets a price, or creates production work by itself: it is staff
    who convert it into a real apps.orders.Order (Подтвердить) or reject it
    (Отклонить). See apps.inbox.services for the conversion logic, which
    reuses apps.orders.services.create_order — never a second order-creation
    path."""

    NEW = "new"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (NEW, _("New")),
        (CONFIRMED, _("Confirmed")),
        (DECLINED, _("Declined")),
        (CANCELLED, _("Cancelled")),
    ]
    OPEN_STATUSES = [NEW]

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    SOURCE_CHOICES = [(TELEGRAM, _("Telegram")), (WHATSAPP, _("WhatsApp"))]

    client = models.ForeignKey(
        "clients.Client",
        verbose_name=_("client"),
        on_delete=models.PROTECT,
        related_name="order_requests",
    )
    status = models.CharField(_("status"), max_length=16, choices=STATUS_CHOICES, default=NEW)
    source = models.CharField(_("source"), max_length=16, choices=SOURCE_CHOICES)
    note = models.CharField(_("note"), max_length=255, blank=True)
    raw_message = models.TextField(
        _("raw message"),
        blank=True,
        help_text=_("The client's own words — kept for a WhatsApp free-text заявка."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("handled by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    handled_at = models.DateTimeField(_("handled at"), null=True, blank=True)
    decline_reason = models.CharField(_("decline reason"), max_length=255, blank=True)
    order = models.OneToOneField(
        "orders.Order",
        verbose_name=_("order"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="request",
    )

    class Meta:
        verbose_name = _("order request")
        verbose_name_plural = _("order requests")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["client", "status"])]

    def __str__(self):
        return f"Заявка №{self.pk} — {self.client}"


class OrderRequestItem(models.Model):
    request = models.ForeignKey(
        OrderRequest, verbose_name=_("request"), on_delete=models.CASCADE, related_name="items"
    )
    variant = models.ForeignKey(
        "inventory.ProductVariant", verbose_name=_("variant"), on_delete=models.PROTECT
    )
    quantity = models.PositiveIntegerField(_("quantity"), default=1)
    note = models.CharField(_("note"), max_length=255, blank=True)

    class Meta:
        verbose_name = _("request item")
        verbose_name_plural = _("request items")

    def __str__(self):
        return f"{self.variant} × {self.quantity}"


class Favourite(models.Model):
    """Per-client wishlist. The AGGREGATE across clients («чаще всего
    добавляют в избранное») is free demand research surfaced to the owner —
    see apps.inbox.services.top_favourited."""

    client = models.ForeignKey(
        "clients.Client",
        verbose_name=_("client"),
        on_delete=models.CASCADE,
        related_name="favourites",
    )
    variant = models.ForeignKey(
        "inventory.ProductVariant", verbose_name=_("variant"), on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("favourite")
        verbose_name_plural = _("favourites")
        constraints = [
            models.UniqueConstraint(fields=["client", "variant"], name="uniq_favourite_per_client")
        ]

    def __str__(self):
        return f"{self.client} ♥ {self.variant}"


class StockAlert(models.Model):
    """«Уведомить о поступлении» — one row per client waiting on one variant.
    notified_at is set once the back-in-stock push fires (see
    apps.inbox.services.notify_back_in_stock) so a client is never pinged
    twice for the same wait."""

    client = models.ForeignKey(
        "clients.Client",
        verbose_name=_("client"),
        on_delete=models.CASCADE,
        related_name="stock_alerts",
    )
    variant = models.ForeignKey(
        "inventory.ProductVariant", verbose_name=_("variant"), on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    notified_at = models.DateTimeField(_("notified at"), null=True, blank=True)

    class Meta:
        verbose_name = _("stock alert")
        verbose_name_plural = _("stock alerts")
        constraints = [
            models.UniqueConstraint(
                fields=["client", "variant"],
                condition=models.Q(notified_at__isnull=True),
                name="uniq_open_stock_alert_per_client",
            )
        ]

    def __str__(self):
        return f"{self.client} waiting on {self.variant}"
