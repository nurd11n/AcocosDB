"""Send a campaign over Telegram, one recipient at a time, recording each send.

Guarantees (see CLAUDE.md — these are not optional):
- Only clients with marketing_consent AND a telegram_chat_id (they pressed
  /start / shared contact) are reached — enforced in campaign_audience.
- Never message a client twice, even across re-runs: a CampaignRecipient row
  with status=sent is skipped.
- Throttle ~1 msg/sec; on HTTP 429 honour the retry_after and retry once.
- Log every send as a client Interaction, and record per-recipient status.

Run: python manage.py send_campaign <campaign_id> [--dry-run]
--dry-run prints the reachable audience and sends nothing.
"""

import json
import logging
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.campaigns.models import Campaign, CampaignRecipient
from apps.campaigns.services import campaign_audience
from apps.clients.models import Interaction

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


def _telegram_send(token, chat_id, text, photo_paths):
    """Returns (ok, retry_after, error). Sends a media group when there are
    photos, a plain message otherwise. Caption rides on the first photo."""
    try:
        if not photo_paths:
            resp = requests.post(
                API.format(token=token, method="sendMessage"),
                data={"chat_id": chat_id, "text": text},
                timeout=30,
            )
        elif len(photo_paths) == 1:
            with open(photo_paths[0], "rb") as fh:
                resp = requests.post(
                    API.format(token=token, method="sendPhoto"),
                    data={"chat_id": chat_id, "caption": text},
                    files={"photo": fh},
                    timeout=60,
                )
        else:
            media, files, handles = [], {}, []
            try:
                for i, path in enumerate(photo_paths[:10]):
                    key = f"photo{i}"
                    item = {"type": "photo", "media": f"attach://{key}"}
                    if i == 0:
                        item["caption"] = text
                    media.append(item)
                    fh = open(path, "rb")
                    handles.append(fh)
                    files[key] = fh
                resp = requests.post(
                    API.format(token=token, method="sendMediaGroup"),
                    data={"chat_id": chat_id, "media": json.dumps(media)},
                    files=files,
                    timeout=90,
                )
            finally:
                for fh in handles:
                    fh.close()
    except requests.RequestException as exc:
        return False, None, str(exc)[:200]

    if resp.status_code == 429:
        retry_after = resp.json().get("parameters", {}).get("retry_after", 1)
        return False, int(retry_after), "rate limited"
    if not resp.ok:
        return False, None, f"HTTP {resp.status_code}: {resp.text[:150]}"
    return True, None, ""


class Command(BaseCommand):
    help = "Send a campaign over Telegram (one message per consenting client)."

    def add_arguments(self, parser):
        parser.add_argument("campaign_id", type=int)
        parser.add_argument(
            "--dry-run", action="store_true", help="Preview audience, send nothing."
        )

    def handle(self, *args, **options):
        try:
            campaign = Campaign.objects.get(pk=options["campaign_id"])
        except Campaign.DoesNotExist as exc:
            raise CommandError(f"Campaign {options['campaign_id']} not found.") from exc

        audience = campaign_audience(campaign)
        self.stdout.write(f"Reachable audience: {len(audience)} client(s).")

        if options["dry_run"]:
            for c in audience:
                self.stdout.write(f"  - {c.name} ({c.phone})")
            self.stdout.write(self.style.WARNING("Dry run — nothing sent."))
            return

        if campaign.channel != Campaign.TELEGRAM:
            raise CommandError("Only Telegram sending is implemented.")
        # Recipients are Clients, reachable only via the public client bot —
        # never the staff bot's token.
        token = settings.TELEGRAM_CLIENT_TOKEN
        if not token:
            raise CommandError("TELEGRAM_CLIENT_TOKEN is empty — cannot send.")

        photo_paths = [p.photo.path for p in campaign.products.all() if p.photo and p.photo.name]

        campaign.status = Campaign.SENDING
        campaign.save(update_fields=["status"])
        sent = failed = skipped = 0

        for client in audience:
            recipient, _created = CampaignRecipient.objects.get_or_create(
                campaign=campaign, client=client
            )
            if recipient.status == CampaignRecipient.SENT:
                skipped += 1
                continue

            ok, retry_after, error = _telegram_send(
                token, client.telegram_chat_id, campaign.text_ru, photo_paths
            )
            if not ok and retry_after:  # 429 — wait it out, retry once
                time.sleep(retry_after)
                ok, _ra, error = _telegram_send(
                    token, client.telegram_chat_id, campaign.text_ru, photo_paths
                )

            if ok:
                recipient.status = CampaignRecipient.SENT
                recipient.sent_at = timezone.now()
                recipient.error = ""
                sent += 1
                Interaction.objects.create(
                    client=client, kind=Interaction.MESSAGE, note=f"Кампания: {campaign.name}"
                )
            else:
                recipient.status = CampaignRecipient.FAILED
                recipient.error = error
                failed += 1
                logger.warning("Campaign %s -> %s failed: %s", campaign.pk, client.pk, error)
            recipient.save()

            time.sleep(1)  # ~1 msg/sec, Telegram-friendly

        campaign.status = Campaign.SENT
        campaign.save(update_fields=["status"])
        self.stdout.write(
            self.style.SUCCESS(f"Done: {sent} sent, {failed} failed, {skipped} already sent.")
        )
