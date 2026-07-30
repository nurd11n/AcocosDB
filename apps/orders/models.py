"""Production orders (Заказы): a client orders items that don't exist yet.

An Order NEVER deducts stock — producing the goods writes PRODUCTION_IN
movements (services.mark_produced), and handing the order over converts it
into a normal confirmed SaleOrder (services.hand_over -> apps.sales.services.
confirm_sale), which is the only thing that ever deducts stock. Until
delivered (or cancelled), an order's items are RESERVED — held back from
being sold to a walk-in (see apps.inventory.services.reserved_by_variant).
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.currency import CURRENCY_CHOICES


class Order(models.Model):
    NEW = "new"
    IN_PRODUCTION = "in_production"
    READY = "ready"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (NEW, _("New")),
        (IN_PRODUCTION, _("In production")),
        (READY, _("Ready")),
        (DELIVERED, _("Delivered")),
        (CANCELLED, _("Cancelled")),
    ]
    # Statuses that still hold a reservation on their items — a delivered
    # order has become a sale (stock already left), a cancelled one never
    # gets produced/delivered, so neither should keep blocking a walk-in.
    OPEN_STATUSES = [NEW, IN_PRODUCTION, READY]

    client = models.ForeignKey(
        "clients.Client",
        verbose_name=_("client"),
        on_delete=models.PROTECT,
        related_name="production_orders",
    )
    currency = models.CharField(
        _("currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
    )
    status = models.CharField(_("status"), max_length=16, choices=STATUS_CHOICES, default=NEW)
    due_date = models.DateField(_("due date"), null=True, blank=True)
    note = models.CharField(_("note"), max_length=255, blank=True)
    # Set once, at handover (services.hand_over) — the confirmed SaleOrder
    # this production order became. Linked both ways: SaleOrder has no field
    # of its own, but is reachable via this row's related_name.
    sale_order = models.OneToOneField(
        "sales.SaleOrder",
        verbose_name=_("sale order"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="production_order",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    delivered_at = models.DateTimeField(_("delivered at"), null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("production order")
        verbose_name_plural = _("production orders")
        ordering = ["due_date", "-created_at"]
        indexes = [
            # The queue and order list both filter/sort by status + due_date.
            models.Index(fields=["status", "due_date"]),
        ]

    def __str__(self):
        return f"Заказ #{self.pk} — {self.client}"

    @property
    def total(self) -> Decimal:
        return sum((i.line_total for i in self.items.all()), Decimal("0"))

    @property
    def is_overdue(self) -> bool:
        """due_date is a plain date (no timezone) compared against Asia/
        Bishkek LOCAL today, never UTC — an order due "today" must not read
        as overdue for the evening hours where UTC has already rolled over."""
        from django.utils import timezone

        if self.due_date is None or self.status in (self.DELIVERED, self.CANCELLED):
            return False
        return self.due_date < timezone.localdate()

    @property
    def fully_produced(self) -> bool:
        items = list(self.items.all())
        return bool(items) and all(i.produced_qty >= i.quantity for i in items)

    @property
    def produced_summary(self) -> str:
        items = list(self.items.all())
        done = sum(min(i.produced_qty, i.quantity) for i in items)
        total = sum(i.quantity for i in items)
        return f"{done} из {total} произведено"


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order, verbose_name=_("order"), on_delete=models.CASCADE, related_name="items"
    )
    variant = models.ForeignKey(
        "inventory.ProductVariant",
        verbose_name=_("product variant"),
        on_delete=models.PROTECT,
        related_name="order_items",
    )
    quantity = models.PositiveIntegerField(_("quantity"), default=1)
    unit_price = models.DecimalField(_("unit price"), max_digits=12, decimal_places=2)
    currency = models.CharField(
        _("currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
    )
    # Written ONLY by services.mark_produced — never typed by hand (CLAUDE.md:
    # "Never store a 'to produce' number" applies here too — this tracks what
    # WAS produced, the queue always computes what's STILL needed).
    produced_qty = models.PositiveIntegerField(_("produced quantity"), default=0)

    class Meta:
        verbose_name = _("order item")
        verbose_name_plural = _("order items")
        indexes = [
            # The production queue aggregates open-order demand by variant.
            models.Index(fields=["variant"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="orderitem_quantity_positive"),
            models.CheckConstraint(
                condition=Q(produced_qty__gte=0), name="orderitem_produced_qty_nonneg"
            ),
            models.CheckConstraint(
                condition=Q(produced_qty__lte=models.F("quantity")),
                name="orderitem_produced_qty_lte_quantity",
            ),
        ]

    def __str__(self):
        return f"{self.variant} × {self.quantity}"

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity

    @property
    def remaining_to_produce(self) -> int:
        return max(self.quantity - self.produced_qty, 0)
