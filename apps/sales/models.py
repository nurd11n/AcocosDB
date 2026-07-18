from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.currency import CURRENCY_CHOICES


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
    currency = models.CharField(
        _("currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
    )
    total = models.DecimalField(
        _("total"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Set automatically when the sale is approved."),
    )
    # Rate FROZEN at confirm time (1 unit of `currency` = rate_to_kgs сом) and the
    # total pre-converted to сом with it. Every aggregate sums total_kgs and never
    # re-converts, so a change to today's rate can't move a historical figure.
    rate_to_kgs = models.DecimalField(_("rate to KGS"), max_digits=12, decimal_places=6, default=1)
    total_kgs = models.DecimalField(
        _("total, KGS"), max_digits=12, decimal_places=2, default=Decimal("0")
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
        indexes = [
            # today_summary / daily report filter by status + confirmed_at date.
            models.Index(fields=["status", "confirmed_at"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(total__gte=0), name="saleorder_total_nonneg"),
        ]

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
        # Only payments in the order's OWN currency count toward its balance —
        # debt/payment matching is always per-currency, never auto-converted.
        return self.payments.filter(currency=self.currency).aggregate(s=Sum("amount"))[
            "s"
        ] or Decimal("0")

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
        indexes = [
            # Dashboard top-products / dead-stock aggregate sale items by variant.
            models.Index(fields=["variant"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="saleitem_quantity_positive"),
        ]

    def __str__(self):
        return f"{self.variant} × {self.quantity}"

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity


class Payment(models.Model):
    """Counts immediately toward revenue and debt the moment it's saved — no
    approval blocking. `reviewed` is the day-end checklist flag, not a gate on
    whether the payment counts. Never deleted: voiding creates a reversing
    entry (see services.void_payment) so the audit trail stays intact."""

    CASH = "cash"
    MBANK = "mbank"
    TRANSFER = "transfer"
    OTHER = "other"
    METHOD_CHOICES = [
        (CASH, _("Cash")),
        (MBANK, "MBank"),
        (TRANSFER, _("Transfer")),
        (OTHER, _("Other")),
    ]

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
    currency = models.CharField(
        _("currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
    )
    # Rate FROZEN when the payment is recorded (null only until the first save
    # snapshots it). Dashboard debt totals compute amount * rate_to_kgs; never
    # re-converted, so historical debt in сом is stable.
    rate_to_kgs = models.DecimalField(
        _("rate to KGS"), max_digits=12, decimal_places=6, null=True, blank=True
    )
    method = models.CharField(_("method"), max_length=16, choices=METHOD_CHOICES, default=CASH)
    note = models.CharField(_("note"), max_length=255, blank=True)
    reviewed = models.BooleanField(_("reviewed"), default=False)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("reviewed by"),
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    reviewed_at = models.DateTimeField(_("reviewed at"), null=True, blank=True)
    reversed_payment = models.ForeignKey(
        "self",
        verbose_name=_("reverses payment"),
        null=True,
        blank=True,
        related_name="reversal",
        on_delete=models.PROTECT,
        help_text=_("Set automatically when this row voids an earlier payment."),
    )
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
        indexes = [
            # Per-client debt/history queries scan by client over time.
            models.Index(fields=["client", "created_at"]),
        ]
        constraints = [
            # Payments are positive; the ONLY exception is a reversal row (see
            # services.void_payment), which is negative and links the payment it
            # voids. Everything else must be a real, positive amount.
            models.CheckConstraint(
                condition=Q(amount__gt=0) | Q(reversed_payment__isnull=False),
                name="payment_amount_positive_unless_reversal",
            ),
        ]

    def save(self, *args, **kwargs):
        # Freeze the rate the first time the payment is written, from whatever
        # path created it (service, admin inline, void). A reversal inherits its
        # original's rate (set explicitly by void_payment) so it cancels exactly.
        if self.rate_to_kgs is None:
            from apps.core.currency import snapshot_rate_to_base
            from django.utils import timezone

            self.rate_to_kgs = snapshot_rate_to_base(self.currency, timezone.localdate())
        super().save(*args, **kwargs)

    @property
    def amount_kgs(self) -> Decimal:
        """The payment converted to сом at its FROZEN rate (never re-converted)."""
        return (self.amount * (self.rate_to_kgs or Decimal("1"))).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.amount} {self.currency} — {self.client}"
