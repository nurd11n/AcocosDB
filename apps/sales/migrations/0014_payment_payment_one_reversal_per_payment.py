"""A payment may be voided at most once.

Before this constraint existed, services.void_payment only checked that the
payment being voided was not ITSELF a reversal — nothing stopped the same
payment being voided twice (a double-tapped admin action), which wrote two
negative rows and drove the client's balance negative by the payment amount.

If any such pair already exists in a live database, the AddConstraint below
would fail with a bare IntegrityError naming only the constraint. The check
in front of it turns that into an actionable message instead: money rows are
never deleted automatically here — an Owner has to decide which reversal is
the real one, because it changes a client's debt.
"""

from django.conf import settings
from django.db import migrations, models


def _reject_existing_double_voids(apps, schema_editor):
    Payment = apps.get_model("sales", "Payment")
    dupes = (
        Payment.objects.filter(reversed_payment__isnull=False)
        .values("reversed_payment_id")
        .annotate(n=models.Count("id"))
        .filter(n__gt=1)
    )
    offenders = [(row["reversed_payment_id"], row["n"]) for row in dupes]
    if offenders:
        detail = ", ".join(f"payment #{pid} has {n} reversals" for pid, n in offenders)
        raise RuntimeError(
            "Cannot apply payment_one_reversal_per_payment: some payments were "
            f"voided more than once ({detail}). Each of those clients' debt is "
            "currently overstated as a credit by the duplicated amount. Delete "
            "the surplus reversal row(s) in /panel/ — keeping exactly one per "
            "voided payment — then re-run migrate."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0006_rename_last_name_to_descriptor"),
        ("orders", "0001_initial"),
        ("sales", "0013_saleitem_discount"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(_reject_existing_double_voids, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("reversed_payment__isnull", False)),
                fields=("reversed_payment",),
                name="payment_one_reversal_per_payment",
            ),
        ),
    ]
