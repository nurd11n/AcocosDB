"""Hard-delete notes/tasks that have been done for 4+ weeks (28 days).

Notes aren't on django-simple-history (unlike Product/ProductVariant/Client/
SaleOrder/Payment) — there's no append-only trail to preserve here, so this is
a real delete, not a reversing entry. Safe to re-run: a note already gone just
matches zero rows the next time.

NOT run via Celery (CLAUDE.md forbids it) and NOT folded into the `scheduler`
container's daily loop — it's a separate, optional host crontab entry so it
can be added/removed independently of the report/rates jobs. See README
"Scheduled jobs" for the exact line.

Run manually: python manage.py purge_completed_notes
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.notes.models import Note

logger = logging.getLogger(__name__)

PURGE_AFTER = timedelta(weeks=4)


class Command(BaseCommand):
    help = "Delete notes/tasks that have been marked done for 4+ weeks (28 days)."

    def handle(self, *args, **options):
        cutoff = timezone.now() - PURGE_AFTER
        # done=True AND completed_at set AND stale — un-completed notes have
        # completed_at=None (see apps.notes.services.apply_done) so they never
        # match here regardless of how long ago they were once completed.
        qs = Note.objects.filter(done=True, completed_at__lte=cutoff)
        count = qs.count()
        qs.delete()
        msg = (
            f"purge_completed_notes: deleted {count} note(s) completed before {cutoff.isoformat()}"
        )
        logger.info(msg)
        self.stdout.write(self.style.SUCCESS(msg))
