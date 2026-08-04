from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.core.permissions import BUSINESS_MODEL_PERMISSIONS, EDITOR, VIEWER


def business_permissions():
    """Every Permission object the explicit BUSINESS_MODEL_PERMISSIONS map
    grants — matched by (app_label, model, codename), never a bare codename,
    so e.g. a hypothetical "view_note" in some unrelated future app can never
    slip in through a name collision."""
    query = Q()
    for app_label, models in BUSINESS_MODEL_PERMISSIONS.items():
        for model_name, actions in models.items():
            codenames = [f"{action}_{model_name}" for action in actions]
            query |= Q(
                content_type__app_label=app_label,
                content_type__model=model_name,
                codename__in=codenames,
            )
    if not query:
        return Permission.objects.none()
    return Permission.objects.filter(query)


class Command(BaseCommand):
    help = "Create the Editor / Viewer groups with their permissions."

    def handle(self, *args, **options):
        perms = business_permissions()

        editor, _ = Group.objects.get_or_create(name=EDITOR)
        editor.permissions.set(perms)

        viewer, _ = Group.objects.get_or_create(name=VIEWER)
        viewer.permissions.set(perms.filter(codename__startswith="view_"))

        self.stdout.write(self.style.SUCCESS("Groups ready: Editor, Viewer"))
        self.stdout.write("Assign users to groups in the panel; give staff status, not superuser.")
