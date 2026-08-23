"""The Owner's PERSONAL (non-business) spending — deliberately isolated from
every business model in this project, on purpose, at every layer:

- Zero foreign keys to anything in apps.sales/inventory/clients/orders/
  manufacturing — a PersonalExpense cannot even be linked to a client, a
  product, or a contractor if someone tried. The only thing it can ever be
  is a date, an amount, a currency, a tag, and a description.
- Never read by apps.core.management.commands.send_daily_report (emailed +
  Telegram'd to staff) — a business report has no reason to carry the
  Owner's personal spending to anyone else who receives it.
- Never enters apps.manufacturing.dashboard.spent_kgs/net_cash/overhead_kgs
  or apps.reports.dashboard's revenue/profit/COGS — a personal purchase is
  not business income or a business expense, and mixing the two would
  quietly misstate the shop's real numbers.
- The ONE dashboard card that reads this (apps.personal.dashboard) computes
  its own figures independently and is never merged into
  apps.reports.dashboard.dashboard_data()'s return value — see that
  module's own docstring for exactly why that boundary matters (it is what
  keeps dashboard_export/dashboard_sheets structurally unable to include it,
  not just a convention someone could forget).
- Owner-only everywhere: admin, /personal/ page, and its export — same
  _owner_only_permissions/is_superuser pattern apps.manufacturing already
  uses, and deliberately absent from
  apps.core.permissions.BUSINESS_MODEL_PERMISSIONS so Editor/Viewer get
  NOTHING here, not even view, the same "safe default" that file documents
  for Campaign/BotContent.

rate_to_kgs follows the exact same freeze discipline as
apps.manufacturing.Expense (see that model's own docstring, and
apps.personal.admin._FrozenRateAdminMixin here) — this was the source of a
real bug there (a hand-typed rate on /panel/ filed 181 000 сом as
15 928 000), so this model is built with the DB constraint from day one
rather than discovering the same defect a second time.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.currency import CURRENCY_CHOICES


class PersonalExpense(models.Model):
    FOOD = "food"
    HOUSING = "housing"
    TRANSPORT = "transport"
    EDUCATION = "education"
    HEALTH = "health"
    SHOPPING = "shopping"
    ENTERTAINMENT = "entertainment"
    SUBSCRIPTIONS = "subscriptions"
    PERSONAL = "personal"
    BUSINESS = "business"
    FEES = "fees"
    TAG_CHOICES = [
        (FOOD, _("Еда")),
        (HOUSING, _("Жильё")),
        (TRANSPORT, _("Транспорт")),
        (EDUCATION, _("Образование")),
        (HEALTH, _("Здоровье")),
        (SHOPPING, _("Покупки")),
        (ENTERTAINMENT, _("Развлечения")),
        (SUBSCRIPTIONS, _("Подписки")),
        (PERSONAL, _("Личное")),
        (BUSINESS, _("Бизнес")),
        (FEES, _("Комиссии")),
    ]

    date = models.DateField(_("date"), default=timezone.localdate)
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    currency = models.CharField(
        _("currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
    )
    # Frozen ONCE at creation (apps.personal.services.record_personal_expense
    # / apps.personal.admin._FrozenRateAdminMixin), never hand-typed, never
    # recomputed on edit — same rule every other money model in this project
    # follows (Payment.rate_to_kgs, Expense.rate_to_kgs).
    rate_to_kgs = models.DecimalField(_("rate to KGS"), max_digits=14, decimal_places=6)
    tag = models.CharField(_("tag"), max_length=16, choices=TAG_CHOICES)
    description = models.CharField(_("description"), max_length=255, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("personal expense")
        verbose_name_plural = _("personal expenses")
        ordering = ["-date", "-created_at"]
        indexes = [models.Index(fields=["tag", "date"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="personal_expense_amount_positive"
            ),
            models.CheckConstraint(
                condition=Q(rate_to_kgs__gt=0), name="personal_expense_rate_positive"
            ),
            # 1 сом IS 1 сом — a base-currency row can only ever have rate 1.
            # Same rule, same reason as apps.manufacturing.Expense's own
            # base_currency_rate_is_one: this is what makes a hand-typed rate
            # inflating a сом amount structurally impossible, not just
            # unlikely, even via raw SQL.
            models.CheckConstraint(
                condition=~Q(currency=settings.CURRENCY) | Q(rate_to_kgs=1),
                name="personal_expense_base_currency_rate_is_one",
            ),
        ]

    def __str__(self):
        return f"{self.get_tag_display()} {self.amount} {self.currency} ({self.date})"

    @property
    def amount_kgs(self) -> Decimal:
        return self.amount * self.rate_to_kgs
