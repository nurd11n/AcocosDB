from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.currency import CENTS, CURRENCY_CHOICES


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
        """Shared classifier so the admin (annotated) and the property agree.
        A remainder of at most PAYMENT_ROUNDING_TOLERANCE (default 1.00 сом) —
        sub-сом residue from currency conversion — counts as fully paid, so a
        16.666...-converted payment doesn't leave a permanent ghost debt."""
        if total <= 0 or paid <= 0:
            return SaleOrder.UNPAID
        if total - paid <= settings.PAYMENT_ROUNDING_TOLERANCE:
            return SaleOrder.PAID
        return SaleOrder.PARTIAL

    @property
    def paid_amount(self) -> Decimal:
        # Every payment's NET (gross minus any change handed back, see
        # Payment.net_applied_kgs) counts toward the balance, converted into
        # THIS order's currency at the rate frozen on each payment (rate_to_kgs
        # = that currency's value in KGS at confirm time). A 16 USD payment on
        # a сом order reduces the сом balance by 16 × the frozen USD rate,
        # minus any сом change given back. Same-currency payments (payment_rate
        # == order_rate) come through unchanged.
        # Iterates .all() rather than .values_list(): values_list ALWAYS issues
        # its own query and ignores an existing prefetch_related("payments"),
        # so the daily report was paying one query per order even though it
        # prefetched them (see apps.sales.services.todays_confirmed_orders).
        # Same fix, same reason as apps.orders.services.order_paid_amount.
        order_rate = self.rate_to_kgs or Decimal("1")
        paid = Decimal("0")
        for payment in self.payments.all():
            net_kgs = payment.amount * (payment.rate_to_kgs or Decimal("1")) - (
                payment.change_amount_kgs or Decimal("0")
            )
            paid += net_kgs / order_rate
        return paid.quantize(CENTS, rounding=ROUND_HALF_UP)

    @property
    def balance(self) -> Decimal:
        """Outstanding amount on this order (0 once fully paid). Only approved
        orders carry a real balance; pending/cancelled ones owe nothing yet. A
        remainder within PAYMENT_ROUNDING_TOLERANCE clamps to exactly 0 — see
        payment_status_for."""
        if self.status != self.CONFIRMED:
            return Decimal("0")
        remainder = self.total - self.paid_amount
        if remainder <= settings.PAYMENT_ROUNDING_TOLERANCE:
            return Decimal("0")
        return remainder

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

    RATE_NBKR = "nbkr"
    RATE_MANUAL = "manual"
    RATE_SOURCE_CHOICES = [(RATE_NBKR, _("NBKR")), (RATE_MANUAL, _("Manual"))]

    # What happens to money handed over beyond the sale's balance. Always a
    # deliberate choice — never auto-picked (see apps.pos.views). 'debt' and
    # 'credit' apply the FULL amount (no change subtracted); the excess then
    # naturally reduces the client's other same-currency debt through the
    # pooled per-client aggregate (apps.clients.services.client_debts_by_currency)
    # — no separate ledger entry needed. They differ only in the label shown
    # to the manager/owner (audit intent), not in the underlying math.
    DISPOSITION_NONE = "none"
    DISPOSITION_CHANGE = "change"
    DISPOSITION_DEBT = "debt"
    DISPOSITION_CREDIT = "credit"
    EXCESS_DISPOSITION_CHOICES = [
        (DISPOSITION_NONE, _("—")),
        (DISPOSITION_CHANGE, _("Сдача")),
        (DISPOSITION_DEBT, _("В счёт долга")),
        (DISPOSITION_CREDIT, _("Аванс")),
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
    # A deposit (аванс) taken on a production order (apps.orders) BEFORE any
    # SaleOrder exists — reuses this same Payment row and its frozen-rate
    # mechanism, never a second payments path (CLAUDE.md). At handover
    # (apps.orders.services.hand_over) `order` is set to the new SaleOrder so
    # SaleOrder.paid_amount picks the deposit up automatically; this field
    # stays set too, for audit ("this payment started life as a deposit").
    production_order = models.ForeignKey(
        "orders.Order",
        verbose_name=_("production order"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deposits",
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
    # Where rate_to_kgs came from. Setting rate_source=manual is Owner-only,
    # enforced server-side (apps.pos.views + admin), never just hidden in the UI.
    rate_source = models.CharField(
        _("rate source"), max_length=16, choices=RATE_SOURCE_CHOICES, default=RATE_NBKR
    )
    # The official NBKR rate at the moment of a manual override, so the spread
    # the owner captured (booth rate vs official) is reconstructable later.
    # Null whenever rate_source=nbkr (there is no spread to record).
    rate_official = models.DecimalField(
        _("official rate"), max_digits=14, decimal_places=6, null=True, blank=True
    )
    method = models.CharField(_("method"), max_length=16, choices=METHOD_CHOICES, default=CASH)
    # Change given back on this payment — always 1:1 with it, never a
    # separate row. change_amount/change_currency are what physically left
    # the till (after any bounded manual adjustment, see services.py);
    # change_amount_kgs is that same figure converted at this payment's OWN
    # frozen rate_to_kgs — never a second rate lookup ("one transaction, one
    # rate"). change_rounding_kgs is the pure floor-to-CHANGE_ROUNDING_STEP
    # residue between the ideal and rounded change (always >= 0, unaffected
    # by any later manual adjustment) — kept for till-drift reporting.
    excess_disposition = models.CharField(
        _("excess disposition"),
        max_length=16,
        choices=EXCESS_DISPOSITION_CHOICES,
        default=DISPOSITION_NONE,
    )
    change_amount = models.DecimalField(
        _("change amount"), max_digits=12, decimal_places=2, default=Decimal("0")
    )
    change_currency = models.CharField(
        _("change currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
    )
    change_amount_kgs = models.DecimalField(
        _("change amount, KGS"), max_digits=12, decimal_places=2, default=Decimal("0")
    )
    change_rounding_kgs = models.DecimalField(
        _("change rounding residue, KGS"), max_digits=12, decimal_places=2, default=Decimal("0")
    )
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
            # Change is always non-negative — a negative figure would mean
            # underpayment, a completely different thing from change. A
            # reversal (services.void_payment) mirrors EVERY money field's
            # sign, including this one, so it cancels the original exactly.
            models.CheckConstraint(
                condition=Q(change_amount__gte=0) | Q(reversed_payment__isnull=False),
                name="payment_change_amount_nonneg_unless_reversal",
            ),
            # Can never hand back more (in KGS terms) than was received.
            models.CheckConstraint(
                condition=Q(change_amount_kgs__lte=F("amount") * F("rate_to_kgs"))
                | Q(reversed_payment__isnull=False),
                name="payment_change_kgs_lte_amount_kgs_unless_reversal",
            ),
            # Rounds DOWN from the ideal figure, so the residue is always
            # non-negative and small — a coarse DB-level sanity net; the exact
            # bound against the LIVE CHANGE_ROUNDING_STEP env value is
            # enforced in services.py (a fixed DB constraint can't track an
            # env var that may change without a new migration).
            models.CheckConstraint(
                condition=(Q(change_rounding_kgs__gte=0) & Q(change_rounding_kgs__lte=100))
                | Q(reversed_payment__isnull=False),
                name="payment_change_rounding_bounded_unless_reversal",
            ),
        ]

    def save(self, *args, **kwargs):
        # Freeze the rate the first time the payment is written, from whatever
        # path created it (service, admin inline, void). A reversal inherits its
        # original's rate (set explicitly by void_payment) so it cancels exactly.
        if self.rate_to_kgs is None:
            from django.utils import timezone

            from apps.core.currency import snapshot_rate_to_base

            self.rate_to_kgs = snapshot_rate_to_base(self.currency, timezone.localdate())
        super().save(*args, **kwargs)

    @property
    def amount_kgs(self) -> Decimal:
        """The payment converted to сом at its FROZEN rate (never re-converted)."""
        return (self.amount * (self.rate_to_kgs or Decimal("1"))).quantize(Decimal("0.01"))

    @property
    def net_applied_kgs(self) -> Decimal:
        """What actually reduces a balance/debt: the gross amount MINUS any
        change handed back, both in сом. This — never the gross amount_kgs —
        is what every balance/debt calculation must sum (CLAUDE.md)."""
        return self.amount_kgs - self.change_amount_kgs

    def __str__(self):
        return f"{self.amount} {self.currency} — {self.client}"
