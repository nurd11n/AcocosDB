"""Abandoned draft sales (started but never approved) pile up as clutter —
they never touched stock, so deleting them is safe. Run daily via cron/scheduler.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.sales.models import SaleOrder


class Command(BaseCommand):
    help = "Delete draft SaleOrders older than 24h that were never approved."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(hours=options["hours"])
        qs = SaleOrder.objects.filter(status=SaleOrder.DRAFT, created_at__lt=cutoff)
        count = qs.count()
        qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} abandoned draft sale(s)."))
