from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from apps.core.permissions import BUSINESS_APPS, EDITOR, VIEWER


class Command(BaseCommand):
    help = "Create the Editor / Viewer groups with their permissions."

    def handle(self, *args, **options):
        perms = Permission.objects.filter(content_type__app_label__in=BUSINESS_APPS)

        editor, _ = Group.objects.get_or_create(name=EDITOR)
        editor.permissions.set(perms.filter(codename__regex=r"^(add|change|view)_"))

        viewer, _ = Group.objects.get_or_create(name=VIEWER)
        viewer.permissions.set(perms.filter(codename__startswith="view_"))

        self.stdout.write(self.style.SUCCESS("Groups ready: Editor, Viewer"))
        self.stdout.write("Assign users to groups in the panel; give staff status, not superuser.")
