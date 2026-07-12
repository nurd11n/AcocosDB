from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords


class SaleOrder(models.Model):
    # DB values kept stable; only the labels shown to users changed to the
    # approval wording (Pending -> Approved) the shop actually uses.
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, _("Pending")),
        (CONFIRMED, _("Approved")),
        (CANCELLED, _("Cancelled")),
    ]

    # Payment status is DERIVED from payments recorded against the order, never
    # stored — same principle as stock and client debt.
    UNPAID = "unpaid"
    PARTIAL = "partial"
    PAID = "paid"

    INSTAGRAM = "instagram"
    WHATSAPP = "whatsapp"
    SHOP = "shop"
    WHOLESALE = "wholesale"
    CHANNEL_CHOICES = [
        (INSTAGRAM, "Instagram"),
        (WHATSAPP, "WhatsApp"),
        (SHOP, _("Shop")),
        (WHOLESALE, _("Wholesale")),
    ]

    client = models.ForeignKey(
        "clients.Client",
        verbose_name=_("client"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sales",
        help_text=_("Leave empty for a walk-in sale."),
    )
    channel = models.CharField(_("channel"), max_length=16, choices=CHANNEL_CHOICES, default=SHOP)
    status = models.CharField(_("status"), max_length=16, choices=STATUS_CHOICES, default=DRAFT)
    total = models.DecimalField(
        _("total"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Set automatically when the sale is approved."),
    )
    note = models.CharField(_("note"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    confirmed_at = models.DateTimeField(_("confirmed at"), null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("sale order")
        verbose_name_plural = _("sale orders")
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.pk} — {self.get_status_display()}"

    @staticmethod
    def payment_status_for(total: Decimal, paid: Decimal) -> str:
        """Shared classifier so the admin (annotated) and the property agree."""
        if total <= 0 or paid <= 0:
            return SaleOrder.UNPAID
        if paid >= total:
            return SaleOrder.PAID
        return SaleOrder.PARTIAL

    @property
    def paid_amount(self) -> Decimal:
        return self.payments.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def balance(self) -> Decimal:
        """Outstanding amount on this order (0 once fully paid). Only approved
        orders carry a real balance; pending/cancelled ones owe nothing yet."""
        if self.status != self.CONFIRMED:
            return Decimal("0")
        return max(self.total - self.paid_amount, Decimal("0"))

    @property
    def payment_status(self) -> str:
        return self.payment_status_for(self.total, self.paid_amount)


class SaleItem(models.Model):
    order = models.ForeignKey(
        SaleOrder, verbose_name=_("sale order"), on_delete=models.CASCADE, related_name="items"
    )
    variant = models.ForeignKey(
        "inventory.ProductVariant",
        verbose_name=_("product variant"),
        on_delete=models.PROTECT,
        related_name="sale_items",
    )
    quantity = models.PositiveIntegerField(_("quantity"), default=1)
    unit_price = models.DecimalField(_("unit price"), max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = _("sale item")
        verbose_name_plural = _("sale items")

    def __str__(self):
        return f"{self.variant} × {self.quantity}"

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity


class Payment(models.Model):
    CASH = "cash"
    TRANSFER = "transfer"
    METHOD_CHOICES = [(CASH, _("Cash")), (TRANSFER, _("Transfer"))]

    client = models.ForeignKey(
        "clients.Client",
        verbose_name=_("client"),
        on_delete=models.PROTECT,
        related_name="payments",
    )
    order = models.ForeignKey(
        SaleOrder,
        verbose_name=_("sale order"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    method = models.CharField(_("method"), max_length=16, choices=METHOD_CHOICES, default=CASH)
    note = models.CharField(_("note"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("payment")
        verbose_name_plural = _("payments")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.amount} — {self.client}"
