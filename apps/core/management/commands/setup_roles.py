from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from apps.core.permissions import MANAGER, OWNER, PROJECT_APPS, VIEWER


class Command(BaseCommand):
    help = "Create the Owner / Manager / Viewer groups with their permissions."

    def handle(self, *args, **options):
        perms = Permission.objects.filter(content_type__app_label__in=PROJECT_APPS)

        owner, _ = Group.objects.get_or_create(name=OWNER)
        owner.permissions.set(perms)

        manager, _ = Group.objects.get_or_create(name=MANAGER)
        manager.permissions.set(
            perms.filter(codename__regex=r"^(add|change|view)_").exclude(
                codename__endswith="_botuser"
            )
        )

        viewer, _ = Group.objects.get_or_create(name=VIEWER)
        viewer.permissions.set(perms.filter(codename__startswith="view_"))

        self.stdout.write(self.style.SUCCESS("Groups ready: Owner, Manager, Viewer"))
        self.stdout.write("Assign users to groups in the panel; give staff status, not superuser.")
