"""Fabric cost, labor cost, contractor balances, defect rate, work-in-
progress — Owner-only throughout, the same visibility tier as cost price and
profit (apps.core.permissions.can_see_costs). Deliberately absent from
apps.core.permissions.BUSINESS_MODEL_PERMISSIONS, so Editor/Viewer get
NOTHING here — not view, not add — even via a crafted /panel/ POST, the same
"safe default" that file already documents for Campaign/BotContent.

Never touches apps.sales.Payment or apps.sales.SaleOrder. A contractor is
owed money BY the shop — the mirror image of a client's debt TO the shop,
not the same relationship — and an expense is cash leaving, not a sale.
Reusing Payment for either would (a) require loosening its non-nullable
`client` FK, an invariant every debt/revenue query relies on, and (b)
silently pollute apps.reports.dashboard's «Получено» query, which sums
EVERY Payment row in a date range with no is_historical-style flag to
exclude a repurposed row — exactly the class of bug that flag was built to
fix, avoidable entirely by keeping the two money flows in separate tables.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.currency import CURRENCY_CHOICES


class Contractor(models.Model):
    """A seamstress/workshop paid for labor — NOT a Client (a customer).
    Kept as its own small model rather than a Client subtype/flag: a
    contractor has no purchase history, debt-owed-TO-the-shop, marketing
    consent, or bot presence — none of Client's fields apply, and conflating
    the two would leak contractor data into every client list/export/bot
    reply that assumes "every Client is a customer"."""

    name = models.CharField(_("name"), max_length=200)
    contact = models.CharField(_("contact"), max_length=200, blank=True)
    note = models.TextField(_("note"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("contractor")
        verbose_name_plural = _("contractors")
        ordering = ["name"]

    def __str__(self):
        return self.name


class ContractorTransaction(models.Model):
    """One entry in a contractor's running balance — the SAME pattern as
    apps.clients.services.client_debts_by_currency (a baseline + signed
    entries, summed live, never stored as a running total), mirrored for the
    opposite money direction: ACCRUAL increases what the shop owes the
    contractor (labor she performed), PAYMENT decreases it (cash handed
    over). Positive apps.manufacturing.services.contractor_balance = shop
    owes contractor; negative = contractor owes shop (e.g. an advance paid
    that hasn't been worked off yet) — same sign convention as
    client_debts_by_currency, just the other party.

    `amount` is always >= 0 (like StockMovement.quantity before its sign
    normalization) — direction comes from `kind`, never a signed amount, so
    a balance query can't be misread by a bare SUM(amount) forgetting to
    flip a sign. Currency is frozen at record time via
    apps.core.currency.snapshot_rate_to_base, the exact same mechanism
    apps.sales.models.Payment.rate_to_kgs and apps.orders.services.
    record_deposit already use — never a second conversion path."""

    ACCRUAL = "accrual"
    PAYMENT = "payment"
    KIND_CHOICES = [
        (ACCRUAL, _("Accrual (owed for labor)")),
        (PAYMENT, _("Payment (cash handed over)")),
    ]

    contractor = models.ForeignKey(
        Contractor,
        verbose_name=_("contractor"),
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    kind = models.CharField(_("kind"), max_length=10, choices=KIND_CHOICES)
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    currency = models.CharField(
        _("currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
    )
    # Frozen at creation — never recomputed, same discipline as
    # Payment.rate_to_kgs (CLAUDE.md: a later rate move must never alter a
    # stored figure).
    rate_to_kgs = models.DecimalField(_("rate to KGS"), max_digits=14, decimal_places=6)
    date = models.DateField(_("date"), default=timezone.localdate)
    note = models.CharField(_("note"), max_length=255, blank=True)
    # Set only when this accrual came from a specific production event — see
    # apps.manufacturing.services.record_production_run. A manual accrual/
    # payment (typed by hand, e.g. a flat monthly rate) has none.
    production_run = models.ForeignKey(
        "ProductionRun",
        verbose_name=_("production run"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contractor_transactions",
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
        verbose_name = _("contractor transaction")
        verbose_name_plural = _("contractor transactions")
        ordering = ["-date", "-created_at"]
        indexes = [models.Index(fields=["contractor", "date"])]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="ctxn_amount_positive"),
            models.CheckConstraint(condition=Q(rate_to_kgs__gt=0), name="ctxn_rate_positive"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount} {self.currency} — {self.contractor}"

    @property
    def amount_kgs(self) -> Decimal:
        return self.amount * self.rate_to_kgs


class ProductionRun(models.Model):
    """One recorded production event for one variant: how many came out
    good (`accepted_qty`) and how many were defective (`defect_qty`) — both
    typed by hand (the only two raw facts a person can actually observe),
    everything else (success rate, absorbed cost) is DERIVED, never typed
    (CLAUDE.md's "stock/revenue/debt are consequences, not hand-typed
    numbers" extended to production quality — see `success_rate` below).

    Deliberately holds NO cost fields of its own — apps.manufacturing.
    services.record_production_run writes the material/labor cost as
    Expense rows linked back here (Expense.production_run), so «Расходы by
    category» never needs a second aggregation source for production cost.
    This row is the anchor three other things link to: the accepted-unit
    StockMovement (PRODUCTION_IN, via its own `reason` text — no FK back,
    matching StockMovement's existing sale_order-only FK shape), the
    Expense rows, and the labor ContractorTransaction.

    `order_item` is optional — a production run started from an Order's
    «Запустить производство» button (apps.orders CLAUDE.md Part 4) links
    back for traceability, but plenty of production is general restocking
    with no order behind it at all."""

    variant = models.ForeignKey(
        "inventory.ProductVariant",
        verbose_name=_("product variant"),
        on_delete=models.PROTECT,
        related_name="production_runs",
    )
    contractor = models.ForeignKey(
        Contractor, verbose_name=_("contractor"), on_delete=models.PROTECT, related_name="runs"
    )
    accepted_qty = models.PositiveIntegerField(_("accepted quantity"), default=0)
    defect_qty = models.PositiveIntegerField(_("defect quantity"), default=0)
    date = models.DateField(_("date"), default=timezone.localdate)
    note = models.CharField(_("note"), max_length=255, blank=True)
    order_item = models.ForeignKey(
        "orders.OrderItem",
        verbose_name=_("order item"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="production_runs",
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
        verbose_name = _("production run")
        verbose_name_plural = _("production runs")
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["contractor", "date"]),
            models.Index(fields=["variant", "date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(accepted_qty__gte=0)
                & Q(defect_qty__gte=0)
                # A run must record SOMETHING — this mirrors StockMovement's
                # own "quantity cannot be zero" rule applied to a whole run.
                & (Q(accepted_qty__gt=0) | Q(defect_qty__gt=0)),
                name="productionrun_qty_nonzero",
            ),
        ]

    def __str__(self):
        return f"{self.variant} × {self.accepted_qty} (брак {self.defect_qty}) — {self.contractor}"

    @property
    def attempted_qty(self) -> int:
        return self.accepted_qty + self.defect_qty

    @property
    def success_rate(self) -> Decimal | None:
        """accepted / (accepted + defect) — COMPUTED, never typed or stored
        (CLAUDE.md: never type a number that's a consequence of others).
        None when nothing was attempted (can't happen given the
        productionrun_qty_nonzero constraint, but a defensive 0 division
        guard costs nothing)."""
        attempted = self.attempted_qty
        if not attempted:
            return None
        return Decimal(self.accepted_qty) / Decimal(attempted)


class Expense(models.Model):
    """The general expense ledger — fabric, trim, labor, rent, everything
    else. TWO structurally distinct category groups (never a per-call-site
    judgment call — see PRODUCTION_CATEGORIES/OVERHEAD_CATEGORIES below),
    because confusing them either double-counts money or hides it:

    - PRODUCTION_CATEGORIES (ткань/фурнитура/зарплата) roll into
      ProductVariant.cost_price -> COGS via record_production_run's cost
      absorption — never subtracted from profit a second time as a raw
      expense total, which would double-count the same сом once through
      COGS-per-unit-sold and once through the expense ledger.
    - OVERHEAD_CATEGORIES (аренда, ONLY) reduce «net profit after overhead»
      DIRECTLY — general costs with no per-unit product to absorb into.
    - прочее (OTHER) is neither: it's real spend, shown in «Потрачено» and
      the by-category breakdown, but affects no profit figure at all,
      because there is no sound assumption to make about what it's for.

    Never counted in revenue or «Получено» — see apps.reports.dashboard's
    _PAY_KGS query, which sums Payment only; Expense is never in that
    union. «Получено» keeps its exact pre-existing meaning."""

    FABRIC = "fabric"
    TRIM = "trim"
    LABOR = "labor"
    RENT = "rent"
    OTHER = "other"
    CATEGORY_CHOICES = [
        (FABRIC, _("Ткань")),
        (TRIM, _("Фурнитура")),
        (LABOR, _("Зарплата")),
        (RENT, _("Аренда")),
        (OTHER, _("Прочее")),
    ]
    # Structural, not a per-site "if category == ...": see class docstring.
    PRODUCTION_CATEGORIES = frozenset({FABRIC, TRIM, LABOR})
    OVERHEAD_CATEGORIES = frozenset({RENT})

    # A SEPARATE, purely DISPLAY grouping — «классификация по зарплате,
    # материалам и прочему», the 3 sections she actually thinks in when
    # scanning a report. Deliberately NOT a replacement for the 5 real
    # categories above: аренда must stay its own category (not folded into
    # прочее) for OVERHEAD_CATEGORIES to keep meaning exactly "аренда, and
    # only аренда" — merging the two here would be a display convenience,
    # merging them in CATEGORY_CHOICES would silently make the net-profit-
    # after-overhead figure sometimes count прочее and sometimes not,
    # depending on what happened to be logged that month.
    SECTION_WAGES = "wages"
    SECTION_MATERIALS = "materials"
    SECTION_OTHER = "other"
    SECTION_LABELS = {
        SECTION_WAGES: _("Зарплата"),
        SECTION_MATERIALS: _("Материалы"),
        SECTION_OTHER: _("Прочее"),
    }
    _SECTION_BY_CATEGORY = {
        LABOR: SECTION_WAGES,
        FABRIC: SECTION_MATERIALS,
        TRIM: SECTION_MATERIALS,
        RENT: SECTION_OTHER,
        OTHER: SECTION_OTHER,
    }

    date = models.DateField(_("date"), default=timezone.localdate)
    category = models.CharField(_("category"), max_length=10, choices=CATEGORY_CHOICES)
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    currency = models.CharField(
        _("currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
    )
    rate_to_kgs = models.DecimalField(_("rate to KGS"), max_digits=14, decimal_places=6)
    contractor = models.ForeignKey(
        Contractor,
        verbose_name=_("contractor / supplier"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
    )
    # Informational tag only — see the module-level reasoning in
    # apps.manufacturing.services.record_production_run's docstring for why
    # a bare variant link on a standalone expense never writes cost_price by
    # itself (there is no sound per-unit quantity to divide by without a
    # ProductionRun).
    variant = models.ForeignKey(
        "inventory.ProductVariant",
        verbose_name=_("product variant"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
    )
    # Set ONLY by record_production_run — never hand-picked in the form, the
    # same discipline as OrderItem.produced_qty (readonly in the admin).
    production_run = models.ForeignKey(
        ProductionRun,
        verbose_name=_("production run"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
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
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("expense")
        verbose_name_plural = _("expenses")
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["category", "date"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="expense_amount_positive"),
            models.CheckConstraint(condition=Q(rate_to_kgs__gt=0), name="expense_rate_positive"),
        ]

    def __str__(self):
        return f"{self.get_category_display()} {self.amount} {self.currency} ({self.date})"

    @property
    def amount_kgs(self) -> Decimal:
        return self.amount * self.rate_to_kgs

    @property
    def is_production_cost(self) -> bool:
        return self.category in self.PRODUCTION_CATEGORIES

    @property
    def is_overhead(self) -> bool:
        return self.category in self.OVERHEAD_CATEGORIES

    @classmethod
    def category_section(cls, category: str) -> str:
        return cls._SECTION_BY_CATEGORY[category]

    @property
    def section(self) -> str:
        return self.category_section(self.category)

    @property
    def section_label(self):
        return self.SECTION_LABELS[self.section]
