from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.currency import CURRENCY_CHOICES


class Client(models.Model):
    INSTAGRAM = "instagram"
    WHATSAPP = "whatsapp"
    SHOP = "shop"
    WHOLESALE = "wholesale"
    SOURCE_CHOICES = [
        (INSTAGRAM, _("Instagram")),
        (WHATSAPP, _("WhatsApp")),
        (SHOP, _("Shop")),
        (WHOLESALE, _("Wholesale")),
    ]

    first_name = models.CharField(_("first name"), max_length=100, default="")
    # NOT a surname — a short free-text tag so staff can tell apart two
    # clients who share a first name ("сестра Розы", "из Instagram", a real
    # surname if that's what's actually distinctive). Never sent to the
    # client: WhatsApp/Telegram messages use first_name alone (see
    # apps.pos.messaging.debt_reminder_text, bot.client_bot's welcome
    # message) — this is for STAFF, reading a list, not client-facing text.
    descriptor = models.CharField(_("descriptor"), max_length=100, blank=True)
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
    # «Не напоминать» on the Должники (debtors) worklist — a per-client skip
    # for a client she's already handling herself (a payment plan agreed by
    # phone, a dispute in progress). Only hides the «Напомнить» action there;
    # the debt itself, the client's own «Напомнить о долге» button on their
    # own page, and «Выписка»/statement generation are all UNAFFECTED — this
    # is a worklist filter, not a debt-collection policy.
    do_not_remind = models.BooleanField(_("do not remind"), default=False)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("client")
        verbose_name_plural = _("clients")
        ordering = ["first_name", "descriptor"]

    def __str__(self):
        return f"{self.name} · {self.phone}"

    @property
    def name(self) -> str:
        """STAFF-facing display name — "Айгуль (сестра Розы)" when a
        descriptor is set, else just "Айгуль". Parenthesised on purpose: it
        reads as a note, not as though `descriptor` were a real surname.
        Client-facing text (WhatsApp, the Telegram bot's welcome message)
        must use `first_name` directly instead of this property — see the
        field's docstring."""
        if self.descriptor:
            return f"{self.first_name} ({self.descriptor})"
        return self.first_name


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


class ClientOpeningBalance(models.Model):
    """Pre-system debt — money a client owed BEFORE this database existed,
    entered once so her running-balance statement
    (apps.clients.services.client_statement) can show the «старый долг» line
    her real paper ledger already has.

    NEVER a SaleOrder (would corrupt revenue/profit/stock — see
    apps.sales.services.confirm_sale, which is the ONLY place stock leaves
    the system for a sale) and NEVER a Payment (would inflate «Получено» on
    the dashboard, which sums Payment.amount with no order filter at all —
    apps.reports.dashboard's `pay` aggregate). This is a THIRD, deliberately
    narrow kind of money record, read in exactly two places:
    client_debts_by_currency (as a starting term, added into that
    currency's running total — nowhere else, per its own docstring) and
    client_statement (as the ledger's very first row, or folded into
    `opening` when a date filter starts after `as_of_date`).

    Owner-only to create or edit (see ClientOpeningBalanceAdmin — the exact
    has_*_permission pattern apps.core.admin.ExchangeRateAdmin already uses
    for the same reason: a money-affecting control an Editor should never
    reach even via a crafted POST). Audit-logged via simple_history, since a
    later dispute over the ORIGINAL figure ("who set this, and when") needs
    a real trail, not just whatever the row currently says.

    One row per (client, currency, as_of_date) — see the constraint below —
    so re-importing the same bulk-import row (apps.clients.management.
    commands.import_opening_balances) upserts in place instead of doubling
    the debt on a second run, and editing the row in the admin is
    unambiguous: there is exactly one place THIS client's THIS currency's
    THIS baseline lives."""

    client = models.ForeignKey(
        Client,
        verbose_name=_("client"),
        on_delete=models.CASCADE,
        related_name="opening_balances",
    )
    # Positive = client owed money before the system existed (the common
    # case — her «старый долг»). Negative is allowed too (a rare client who
    # had pre-paid before go-live) — same sign convention as every other
    # balance in this app (client_debts_by_currency, SaleOrder.balance):
    # positive = owes, negative = credit.
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    currency = models.CharField(
        _("currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
    )
    as_of_date = models.DateField(_("as of date"))
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
        verbose_name = _("opening balance")
        verbose_name_plural = _("opening balances")
        ordering = ["-as_of_date"]
        constraints = [
            # Backs the bulk import's own idempotency (never rely on
            # application-level upsert logic alone — same discipline as
            # every money constraint elsewhere in this app, enforced even
            # against a bulk_create that bypasses full_clean()). Also what
            # makes "editing ONE row recomputes the balance everywhere"
            # unambiguous: there's only ever one row for a given
            # (client, currency, as_of_date).
            models.UniqueConstraint(
                fields=["client", "currency", "as_of_date"],
                name="one_opening_balance_per_client_currency_date",
            ),
            models.CheckConstraint(condition=~models.Q(amount=0), name="opening_balance_nonzero"),
        ]

    def __str__(self):
        return f"{self.client} — {self.amount} {self.currency} ({self.as_of_date})"


class HistoricalPurchase(models.Model):
    """A purchase a client made BEFORE this database existed — free-text, so
    the pre-system transfer works even when the item no longer exists as a
    tracked ProductVariant/SKU at all. Purely informational: shown merged
    into «История покупок» on the client page (apps.pos.views.client_detail),
    each row marked «до системы» so staff never mistake it for a real,
    stock-backed sale.

    NEVER a SaleOrder (would decrement CURRENT stock for an item that may
    not even exist in today's catalog, and would corrupt revenue/profit —
    see apps.sales.services.confirm_sale, the ONLY place stock actually
    leaves the system) and NEVER counted in client_debts_by_currency or
    client_statement — this is about WHAT she bought, not what she owes;
    ClientOpeningBalance already covers pre-system debt and stays the only
    money-affecting pre-system record. `amount` here is shown for context
    only (so the row reads like a real purchase line), never summed into
    any financial total anywhere in the app.

    Owner-only to create or edit — same tier and the same explicit
    is_superuser has_*_permission pattern as ClientOpeningBalanceAdmin/
    ExchangeRateAdmin, even though this doesn't move money itself: it's
    bulk-imported client history, not a figure an Editor should be able to
    plant on a client's record via a crafted POST. Audit-logged via
    simple_history for the same reason.

    UniqueConstraint on the full row (client, date, description, amount,
    currency) is deliberate content-based dedup, not a real-world identity
    key — unlike ClientOpeningBalance's (client, currency, as_of_date),
    which is a single snapshot VALUE that naturally upserts, a purchase is
    an EVENT and the same client can have several genuinely different
    purchases on the same day. This only catches an EXACT re-run of the
    same import row (apps.clients.management.commands.
    import_purchase_history) landing twice; it does NOT support "correct
    a typo by re-importing" the way opening balances do — a typo'd row is
    fixed in the admin, not by re-running the file with different text."""

    client = models.ForeignKey(
        Client,
        verbose_name=_("client"),
        on_delete=models.CASCADE,
        related_name="historical_purchases",
    )
    purchase_date = models.DateField(_("purchase date"))
    # Free text on purpose — see the model docstring. "3 вечерних платья L,
    # 2 карнавальных костюма", not a structured line-item list.
    description = models.CharField(_("description"), max_length=500)
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    currency = models.CharField(
        _("currency"), max_length=3, choices=CURRENCY_CHOICES, default=settings.CURRENCY
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
        verbose_name = _("historical purchase")
        verbose_name_plural = _("historical purchases")
        ordering = ["-purchase_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "purchase_date", "description", "amount", "currency"],
                name="unique_historical_purchase_row",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="historical_purchase_amount_positive"
            ),
        ]

    def __str__(self):
        return f"{self.client} — {self.description} ({self.purchase_date})"
