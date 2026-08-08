"""Hard-delete SecurityEvent rows older than 90 days.

SecurityEvent (failed logins, 403s, rate-limit blocks) is written by traffic
the shop does not control — a scanner, a credential-stuffing run, a bot. It
was the only growing table in this app with NO retention policy at all, while
every comparable one already had one: notes purge at 28 days
(purge_completed_notes), draft sales at 24h (cleanup_draft_sales), backups on
a tiered schedule, request counters on an 8-day cache TTL. Left unbounded it
grows fastest exactly when under attack, in the SAME database that holds
sales, clients and debts — so an abuse-log table nobody reads could fill the
disk and take the actual business down with it.

90 days is well past the daily digest's window (send_security_digest only
ever looks at TODAY) and past any realistic "what happened last month?"
question, while keeping the table bounded by traffic rather than by time
since deploy.

Deleted in batches so a large first run can't hold one long transaction open
against the production DB.

Safe to re-run: already-deleted rows just match zero the next time. Runs
nightly from scheduler.py alongside the digest that reads this table.
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import SecurityEvent

logger = logging.getLogger(__name__)

RETAIN = timedelta(days=90)
BATCH_SIZE = 5000


class Command(BaseCommand):
    help = "Delete SecurityEvent rows older than 90 days (abuse log retention)."

    def handle(self, *args, **options):
        cutoff = timezone.now() - RETAIN
        deleted = 0
        while True:
            batch = list(
                SecurityEvent.objects.filter(created_at__lt=cutoff).values_list("pk", flat=True)[
                    :BATCH_SIZE
                ]
            )
            if not batch:
                break
            removed, _ = SecurityEvent.objects.filter(pk__in=batch).delete()
            deleted += removed
        msg = f"purge_security_events: deleted {deleted} row(s) older than {cutoff.isoformat()}"
        logger.info(msg)
        self.stdout.write(self.style.SUCCESS(msg))
