"""Abandoned draft sales (started but never approved) pile up as clutter —
they never touched stock, so deleting them is safe. Run via cron/scheduler.

Two thresholds, not one. A draft is created the moment /pos/ is opened (so a
reload never loses the basket — CLAUDE.md), which means an EMPTY draft (no
client, no items — someone opened the page and did nothing else) is pure
visual noise from the first minute: there is nothing in it that could ever be
"recovered" by a reload, so there is no reason to wait 24h before it's gone
from the Продажи list. A draft that actually has items or a client attached is
real in-progress work and keeps the original 24h grace period — a manager away
from the till for an hour must not come back to find her basket purged.
"""

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.sales.models import SaleOrder


class Command(BaseCommand):
    help = "Delete abandoned draft SaleOrders: empty ones fast, in-progress ones after 24h."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours", type=int, default=24, help="Grace period for a draft with items/a client."
        )
        parser.add_argument(
            "--empty-hours",
            type=int,
            default=1,
            help="Grace period for a draft with neither items nor a client attached.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        cutoff = now - timezone.timedelta(hours=options["hours"])
        empty_cutoff = now - timezone.timedelta(hours=options["empty_hours"])

        # Resolve to a plain list of pks FIRST (a `.distinct()` values_list is
        # a normal SELECT, no restriction), then delete by pk — a direct
        # `.filter(Q(client...)|Q(items...)).distinct().delete()` fails: the
        # `items` join can duplicate a draft with 2+ lines, `.distinct()`
        # collapses that back down for a SELECT, but Django refuses `.delete()`
        # on a queryset that has `.distinct()` applied at all.
        empty_ids = list(
            SaleOrder.objects.filter(
                status=SaleOrder.DRAFT,
                client__isnull=True,
                items__isnull=True,
                created_at__lt=empty_cutoff,
            ).values_list("pk", flat=True)
        )
        in_progress_ids = list(
            SaleOrder.objects.filter(status=SaleOrder.DRAFT, created_at__lt=cutoff)
            .filter(Q(client__isnull=False) | Q(items__isnull=False))
            .values_list("pk", flat=True)
            .distinct()
        )

        SaleOrder.objects.filter(pk__in=empty_ids).delete()
        SaleOrder.objects.filter(pk__in=in_progress_ids).delete()

        total = len(empty_ids) + len(in_progress_ids)
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {total} abandoned draft sale(s) "
                f"({len(empty_ids)} empty, {len(in_progress_ids)} in-progress)."
            )
        )
