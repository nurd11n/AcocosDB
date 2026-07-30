"""Audience selection for a campaign. The hard floor — reachable Telegram chat
AND marketing consent — is ALWAYS applied; the Campaign's filter flags narrow it
further, and the 2/week frequency cap is applied LAST, unconditionally — never
optional, never a UI toggle (CLAUDE.md/CLIENT_BOTS.md §5.4: "enforced in code —
not policy, code"). Keeping this in one place means the preview count in the
admin and the actual send command can never disagree about who gets a message.
"""

from datetime import timedelta

from django.db.models import Max
from django.utils import timezone

from apps.clients.models import Client
from apps.sales.models import SaleOrder

MAX_BROADCASTS_PER_CLIENT_PER_WEEK = 2


def _over_frequency_cap() -> set[int]:
    """Client IDs already at MAX_BROADCASTS_PER_CLIENT_PER_WEEK sent
    CampaignRecipients (any campaign, either channel) in the last 7 days."""
    from django.db.models import Count

    from .models import CampaignRecipient

    cutoff = timezone.now() - timedelta(days=7)
    rows = (
        CampaignRecipient.objects.filter(status=CampaignRecipient.SENT, sent_at__gte=cutoff)
        .values("client_id")
        .annotate(n=Count("id"))
    )
    return {r["client_id"] for r in rows if r["n"] >= MAX_BROADCASTS_PER_CLIENT_PER_WEEK}


def campaign_audience(campaign) -> list[Client]:
    """The clients a campaign will actually reach, after every filter. The
    hard floor is channel-specific: Telegram can only ever message someone
    who pressed /start (a chat_id); WhatsApp broadcasts are template-only and
    require an explicit whatsapp_opted_in, never just an inbound message."""
    from .models import Campaign

    qs = Client.objects.filter(is_active=True, marketing_consent=True)
    if campaign.channel == Campaign.WHATSAPP:
        qs = qs.filter(whatsapp_opted_in=True).exclude(phone="")
    else:
        qs = qs.filter(telegram_chat_id__isnull=False)

    if campaign.only_favourited_product_id:
        from apps.inbox.models import Favourite

        favourited = Favourite.objects.filter(
            variant__product_id=campaign.only_favourited_product_id
        ).values_list("client_id", flat=True)
        qs = qs.filter(pk__in=set(favourited))

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

    qs = qs.exclude(pk__in=_over_frequency_cap())
    return list(qs.order_by("first_name", "last_name"))


def campaign_preview_count(campaign) -> int:
    return len(campaign_audience(campaign))
