"""Audience selection for a campaign. The hard floor — reachable Telegram chat
AND marketing consent — is ALWAYS applied; the Campaign's filter flags narrow it
further. Keeping this in one place means the preview count in the admin and the
actual send command can never disagree about who gets a message.
"""

from datetime import timedelta

from django.db.models import Max
from django.utils import timezone

from apps.clients.models import Client
from apps.sales.models import SaleOrder


def campaign_audience(campaign) -> list[Client]:
    """The clients a campaign will actually reach, after every filter."""
    qs = Client.objects.filter(
        is_active=True,
        marketing_consent=True,
        telegram_chat_id__isnull=False,  # the only way Telegram lets us message
    )

    if campaign.only_with_debt:
        from apps.clients.services import client_debts_by_currency

        with_debt = {
            cid
            for cid, currs in client_debts_by_currency().items()
            if any(a > 0 for a in currs.values())
        }
        qs = qs.filter(pk__in=with_debt)

    # "Bought before" and "inactive for N days" both look at confirmed purchases.
    if campaign.only_bought_before or campaign.lapsed_days:
        last_purchase = {
            row["client_id"]: row["last"]
            for row in SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, client__isnull=False)
            .values("client_id")
            .annotate(last=Max("confirmed_at"))
        }
        if campaign.only_bought_before:
            qs = qs.filter(pk__in=last_purchase.keys())
        if campaign.lapsed_days:
            cutoff = timezone.now() - timedelta(days=campaign.lapsed_days)
            lapsed = {cid for cid, last in last_purchase.items() if last and last < cutoff}
            qs = qs.filter(pk__in=lapsed)

    return list(qs.order_by("first_name", "last_name"))


def campaign_preview_count(campaign) -> int:
    return len(campaign_audience(campaign))
