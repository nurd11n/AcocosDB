"""Abandoned draft sales pile up as clutter — they never touched stock, so
deleting them is safe. Run daily via cron/scheduler.

Only a TRULY empty draft qualifies: no client, no items, no payments (the
last is implied by no-client anyway, since Payment.client is non-nullable —
kept explicit so this stays correct even if that ever changes). /pos/'s draft
is now created lazily on the first real action (adding an item or picking a
client — see apps.pos.views.sale_new/_get_or_create_pending_draft), so a
draft this command would ever find empty is already a narrow edge case (e.g.
every item was removed again after creation), not the routine "opened the
page and did nothing" clutter it used to be.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.sales.models import SaleOrder


class Command(BaseCommand):
    help = "Delete empty draft SaleOrders (no client, no items, no payments) older than 24h."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(hours=options["hours"])
        qs = SaleOrder.objects.filter(
            status=SaleOrder.DRAFT,
            client__isnull=True,
            items__isnull=True,
            payments__isnull=True,
            created_at__lt=cutoff,
        )
        count = qs.count()
        qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} abandoned draft sale(s)."))
