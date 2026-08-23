from decimal import Decimal

from apps.core.currency import snapshot_rate_to_base

from .models import PersonalExpense


def record_personal_expense(
    *, date, tag: str, amount: Decimal, currency: str, description: str = ""
) -> PersonalExpense:
    """The ONE way a PersonalExpense is ever created outside /panel/ (which
    freezes the rate itself via _FrozenRateAdminMixin) — freezes rate_to_kgs
    the same way apps.manufacturing.services.record_expense does, so this
    row's KGS value can never move if today's rate changes later."""
    rate = snapshot_rate_to_base(currency, date)
    return PersonalExpense.objects.create(
        date=date,
        tag=tag,
        amount=amount,
        currency=currency,
        rate_to_kgs=rate,
        description=description,
    )
