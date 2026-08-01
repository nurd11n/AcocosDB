# last_name -> descriptor: it was never a real surname requirement (blank=True,
# free text) — it's a short staff-facing tag to tell apart two clients sharing
# a first name. RenameField preserves every existing value (a straight
# ALTER TABLE ... RENAME COLUMN, no data touched, no RunPython needed) and
# keeps ordering/search behaviour identical; only the label and Client.name's
# display format change (see apps/clients/models.py).

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0005_client_bot_language_client_human_handoff_until_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="client",
            old_name="last_name",
            new_name="descriptor",
        ),
        migrations.RenameField(
            model_name="historicalclient",
            old_name="last_name",
            new_name="descriptor",
        ),
        migrations.AlterField(
            model_name="client",
            name="descriptor",
            field=models.CharField(blank=True, max_length=100, verbose_name="descriptor"),
        ),
        migrations.AlterField(
            model_name="historicalclient",
            name="descriptor",
            field=models.CharField(blank=True, max_length=100, verbose_name="descriptor"),
        ),
        migrations.AlterModelOptions(
            name="client",
            options={
                "ordering": ["first_name", "descriptor"],
                "verbose_name": "client",
                "verbose_name_plural": "clients",
            },
        ),
    ]
