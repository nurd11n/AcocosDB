"""Role helpers. Two groups, created by `manage.py setup_roles`:

- Editor — add/change/view on business models only. No deletes, no cost prices.
- Viewer — view-only on business models. No cost prices.

Superuser is Django's built-in flag, not a group: it gets everything, including
Users, Groups, bot users, WhatsApp/bot logs, request stats, and cost prices.
"""

EDITOR = "Editor"
VIEWER = "Viewer"

# Only these apps' models are ever granted to Editor/Viewer. Everything else
# (core, auth, axes, sessions) stays superuser-only — which also keeps
# those apps out of the Editor/Viewer sidebar entirely, since Django's admin only
# lists apps a user has at least one permission in. "reports" is included so
# Editor/Viewer can see the day-end review list — the ModelAdmin itself still
# blocks editing/reviewing for non-superusers regardless of this DB permission.
BUSINESS_APPS = ["inventory", "clients", "sales", "orders", "reports", "inbox"]


def can_see_costs(user) -> bool:
    """Cost price and profit are superuser-only."""
    return bool(user.is_authenticated and user.is_superuser)
