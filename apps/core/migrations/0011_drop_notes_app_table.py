"""apps.notes (Заметки) deleted entirely 2026-08 — unused, the client never
used it. Removing the app from INSTALLED_APPS (and deleting apps/notes/
along with it) leaves Django with no migrations for that app any more, so
there is nothing left to run a normal `notes` migration to drop its own
table — this is the cleanup, living here instead since apps.core is the
natural home for a cross-app teardown. IF EXISTS makes this safe to run
whether or not this database ever had the table (a fresh install never did).
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_securityevent"),
    ]

    operations = [
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS notes_note;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
