"""S1 remediation: `setup_roles` used to grant Editor/Viewer every permission
on every model in a handful of apps by app_label wildcard
(`content_type__app_label__in=BUSINESS_APPS`). That over-granted
inventory.add_stockmovement — StockMovementAdmin blocks change/delete but
never had has_add_permission, so the wildcard-granted Django permission let
a crafted POST to /panel/inventory/stockmovement/add/ mint phantom stock.

apps.core.permissions.BUSINESS_MODEL_PERMISSIONS replaces the wildcard with
an explicit (app_label, model) -> {actions} allowlist going forward, but a
code change alone doesn't touch permissions already sitting on Group rows in
an existing database — `setup_roles` has to be RE-RUN for that, and nothing
guarantees an already-deployed installation will do that promptly. This data
migration re-applies the same explicit set to any Editor/Viewer group that
already exists, so upgrading is what revokes the over-grant, not a manual
step someone has to remember.
"""

from django.db import migrations


EDITOR = "Editor"
VIEWER = "Viewer"

# Mirrors apps.core.permissions.BUSINESS_MODEL_PERMISSIONS at the time this
# migration was written. Deliberately NOT imported live — a migration must
# stay correct even if that map changes shape later; this is a frozen
# snapshot of what Editor/Viewer should have had at THIS point in history.
BUSINESS_MODEL_PERMISSIONS = {
    "inventory": {
        "category": {"add", "change", "view"},
        "product": {"add", "change", "view"},
        "productimage": {"add", "change", "view"},
        "productvariant": {"add", "change", "view"},
    },
    "clients": {
        "client": {"add", "change", "view"},
        "interaction": {"add", "change", "view"},
    },
    "sales": {
        "saleorder": {"add", "change", "view"},
        "saleitem": {"add", "change", "view"},
    },
    "orders": {
        "order": {"add", "change", "view"},
        "orderitem": {"add", "change", "view"},
    },
    "inbox": {
        "orderrequest": {"add", "change", "view"},
        "favourite": {"view"},
        "stockalert": {"view"},
    },
    # "notes" entry removed 2026-08 along with the app itself — this dict is
    # a frozen historical snapshot (see module docstring), but the migration
    # DEPENDENCY on ("notes", "0002_note_completed_at") below had to go too:
    # Django's migration graph errors on a dependency naming an app that no
    # longer exists. The permission query below simply matches nothing for
    # an app_label with no content type, so dropping the dict entry changes
    # no behavior — only the now-impossible dependency required a real edit.
}


def revoke_wildcard_grants(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    from django.db.models import Q

    query = Q()
    for app_label, models in BUSINESS_MODEL_PERMISSIONS.items():
        for model_name, actions in models.items():
            codenames = [f"{action}_{model_name}" for action in actions]
            query |= Q(
                content_type__app_label=app_label,
                content_type__model=model_name,
                codename__in=codenames,
            )
    correct_perms = Permission.objects.filter(query) if query else Permission.objects.none()

    editor = Group.objects.filter(name=EDITOR).first()
    if editor is not None:
        editor.permissions.set(correct_perms)

    viewer = Group.objects.filter(name=VIEWER).first()
    if viewer is not None:
        viewer.permissions.set(correct_perms.filter(codename__startswith="view_"))


def noop_reverse(apps, schema_editor):
    # Deliberately a no-op: reversing would mean re-granting the wildcard
    # (including inventory.add_stockmovement) — the whole point of this
    # migration is that that grant should never come back.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_frankfurter_rate_source"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("inventory", "0007_alter_productimage_position"),
        ("clients", "0006_rename_last_name_to_descriptor"),
        ("sales", "0018_payment_payment_must_be_linked_to_order_or_deposit"),
        ("orders", "0001_initial"),
        ("inbox", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(revoke_wildcard_grants, noop_reverse),
    ]
