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
# lists apps a user has at least one permission in. "reports" was here for
# DailyReview, the payment-review queue's proxy model — removed 2026-08 along
# with the whole review feature, and apps.reports has held no model since, so
# it's dropped from this list too (it granted nothing regardless of presence).
# "notes" is here so the shared scratchpad follows the same rule as everything
# else: Editor writes, Viewer reads. Before it was listed, NO role held a notes
# permission and the views checked none — so a Viewer could create, edit and
# permanently DELETE another user's note (apps/notes/views.py now gates on
# these, and Editor still gets no delete_ since the regex grants add/change/view
# only — deleting is Owner-only, like every other business model).
BUSINESS_APPS = ["inventory", "clients", "sales", "orders", "inbox", "notes"]


def can_see_costs(user) -> bool:
    """Cost price and profit are superuser-only."""
    return bool(user.is_authenticated and user.is_superuser)
