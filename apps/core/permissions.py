"""Role helpers. Three roles, created by `manage.py setup_roles`:

- Owner   — everything, including cost prices and profit.
- Manager — add/change/view inventory, clients, sales. No deletes, no users, no costs.
- Viewer  — read-only everywhere.
"""

OWNER = "Owner"
MANAGER = "Manager"
VIEWER = "Viewer"

PROJECT_APPS = ["core", "inventory", "clients", "sales", "wa"]


def can_see_costs(user) -> bool:
    """Cost price and profit are Owner-only."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    cached = getattr(user, "_can_see_costs", None)
    if cached is None:
        cached = user.groups.filter(name=OWNER).exists()
        user._can_see_costs = cached
    return cached
