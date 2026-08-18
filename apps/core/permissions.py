"""Role helpers. Two groups, created by `manage.py setup_roles`:

- Editor — add/change/view on business models only. No deletes, no cost prices.
- Viewer — view-only on business models. No cost prices.

Superuser is Django's built-in flag, not a group: it gets everything, including
Users, Groups, bot users, WhatsApp/bot logs, request stats, and cost prices.
"""

EDITOR = "Editor"
VIEWER = "Viewer"

# Explicit (app_label, model) -> {actions} grants for Editor. Viewer gets the
# "view" subset of the same map. This replaces a wildcard-by-app-label grant
# (`Permission.objects.filter(content_type__app_label__in=BUSINESS_APPS)`),
# which silently handed Editor add/change on EVERY model in these apps,
# including ones that are only safe because their own ModelAdmin blocks the
# action separately — StockMovement is the concrete case that slipped
# through: its admin blocks change/delete but never had has_add_permission,
# so a raw crafted POST to /panel/inventory/stockmovement/add/ could mint
# phantom stock, exactly because the wildcard had already granted
# inventory.add_stockmovement to every Editor. A model added to any of these
# apps in the future now grants NOTHING to Editor/Viewer until someone
# deliberately adds it here — the safe default, instead of the wildcard's
# "everything is granted unless something else happens to block it."
#
# Every entry below is add/change/view except where a narrower comment
# explains why — never delete (Owner-only everywhere, per CLAUDE.md) and
# never a model whose own admin already fully Owner-gates it (BotContent,
# like Campaign, stays out entirely — Editor gets nothing there, not even
# view, matching CLAUDE.md's "Owner-only to edit").
BUSINESS_MODEL_PERMISSIONS: dict[str, dict[str, set[str]]] = {
    "inventory": {
        "category": {"add", "change", "view"},
        "product": {"add", "change", "view"},
        "productimage": {"add", "change", "view"},
        "productvariant": {"add", "change", "view"},
        # StockMovement: append-only ledger, written ONLY via
        # apps.inventory.services.add_movement (the Принять/Списать/
        # Пересчёт buttons on the variant list) — deliberately absent here.
    },
    "clients": {
        "client": {"add", "change", "view"},
        "interaction": {"add", "change", "view"},
    },
    "sales": {
        "saleorder": {"add", "change", "view"},
        "saleitem": {"add", "change", "view"},
        # Payment: created ONLY via services.record_payment/record_deposit
        # (the sale's own payment panel, or a production-order deposit) —
        # deliberately absent here; PaymentAdmin also independently blocks
        # add/change for everyone (see apps/sales/admin.py), this is just
        # the matching permission-side half of that same rule.
    },
    "orders": {
        "order": {"add", "change", "view"},
        "orderitem": {"add", "change", "view"},
    },
    "inbox": {
        "orderrequest": {"add", "change", "view"},
        # confirm/decline (apps.inbox.services.confirm_order_request) is
        # gated on inbox.add_orderrequest itself, per CLAUDE.md — the "add"
        # grant here is load-bearing for that check, not just admin access.
        "favourite": {"view"},  # created by clients via the bots, not staff
        "stockalert": {"view"},  # ditto — FavouriteAdmin/StockAlertAdmin
        # also block add/change/delete outright, this mirrors that.
        # BotContent: Owner-only, like Campaign — system-wide bot copy.
    },
    # apps.notes (Заметки) removed 2026-08 — unused, deleted entirely rather
    # than kept around "just in case" (do not resurrect this entry without
    # the app itself existing again).
}


def can_see_costs(user) -> bool:
    """Cost price and profit are superuser-only."""
    return bool(user.is_authenticated and user.is_superuser)
