"""Send a campaign, one recipient at a time, recording each send.

Guarantees (see CLAUDE.md / CLIENT_BOTS.md §5 — these are not optional):
- Only clients with marketing_consent AND a telegram_chat_id (they pressed
  /start / shared contact) are reached — enforced in campaign_audience, which
  ALSO excludes anyone already at the 2-broadcasts-per-week cap.
- Never message a client twice, even across re-runs: a CampaignRecipient row
  with status=sent is skipped.
- Never sends between 22:00-09:00 Asia/Bishkek — queue it for the morning.
- Telegram: throttle ~20 msg/sec; on HTTP 429 honour retry_after exactly,
  retry up to 3x with backoff, then mark that recipient failed and continue —
  one bad chat never stalls a campaign.
- WhatsApp: approved template only, opted-in clients only, feature-gated off
  by default (WHATSAPP_BROADCAST_ENABLED) — never free-form bulk.
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
TELEGRAM_THROTTLE_SECONDS = 0.05  # ~20 msg/sec
MAX_RETRIES = 3
QUIET_HOURS_START = 22  # 22:00 Asia/Bishkek
QUIET_HOURS_END = 9  # 09:00 Asia/Bishkek


def _within_quiet_hours() -> bool:
    hour = timezone.localtime().hour
    return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


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


def _telegram_send_with_retry(token, chat_id, text, photo_paths):
    ok, retry_after, error = _telegram_send(token, chat_id, text, photo_paths)
    attempts = 0
    while not ok and retry_after and attempts < MAX_RETRIES:
        time.sleep(retry_after)
        ok, retry_after, error = _telegram_send(token, chat_id, text, photo_paths)
        attempts += 1
    return ok, error


def _whatsapp_template_send(
    to: str, template_name: str, hero_image_url: str | None
) -> tuple[bool, str]:
    """One approved template, one hero image, one CTA — the cost pattern that
    matters (CLIENT_BOTS.md §5.3): when the client replies, the 24h window
    opens and the rest of the photo set can go free-form at no extra cost."""
    components = []
    if hero_image_url:
        components.append(
            {"type": "header", "parameters": [{"type": "image", "image": {"link": hero_image_url}}]}
        )
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "ru"},
            "components": components,
        },
    }
    url = f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        return False, str(exc)[:200]
    if not resp.ok:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    return True, ""


class Command(BaseCommand):
    help = "Send a campaign over Telegram or WhatsApp (one message per consenting client)."

    def add_arguments(self, parser):
        parser.add_argument("campaign_id", type=int)
        parser.add_argument(
            "--dry-run", action="store_true", help="Preview audience, send nothing."
        )
        parser.add_argument(
            "--ignore-quiet-hours",
            action="store_true",
            help="For tests/manual override only — never pass this from the scheduler.",
        )

    def handle(self, *args, **options):
        if not settings.CAMPAIGNS_ENABLED:
            raise CommandError(
                "CAMPAIGNS_ENABLED=False — campaigns are not production-ready yet, refusing to run."
            )
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

        if _within_quiet_hours() and not options["ignore_quiet_hours"]:
            self.stdout.write(
                self.style.WARNING(
                    "It's quiet hours (22:00-09:00 Asia/Bishkek) — nothing sent. "
                    "Re-run this command in the morning."
                )
            )
            return

        if campaign.channel == Campaign.WHATSAPP:
            self._send_whatsapp(campaign, audience)
        else:
            self._send_telegram(campaign, audience)

    def _send_telegram(self, campaign, audience):
        # Recipients are Clients, reachable only via the public client bot —
        # never the staff bot's token.
        token = settings.TELEGRAM_CLIENT_TOKEN
        if not token:
            raise CommandError("TELEGRAM_CLIENT_TOKEN is empty — cannot send.")

        # prefetch_related so Product.grid_image (a product can have several
        # images now) reads the cache instead of issuing one query per
        # selected product — see its docstring.
        products = list(campaign.products.prefetch_related("images"))
        photo_paths = [img.path for p in products if (img := p.grid_image)]

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

            ok, error = _telegram_send_with_retry(
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

            time.sleep(TELEGRAM_THROTTLE_SECONDS)

        campaign.status = Campaign.SENT
        campaign.save(update_fields=["status"])
        self.stdout.write(
            self.style.SUCCESS(f"Done: {sent} sent, {failed} failed, {skipped} already sent.")
        )

    def _send_whatsapp(self, campaign, audience):
        if not settings.WHATSAPP_BROADCAST_ENABLED:
            raise CommandError(
                "WhatsApp broadcasts are disabled (WHATSAPP_BROADCAST_ENABLED=False) — "
                "never free-form bulk WhatsApp."
            )
        if not (settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID):
            raise CommandError("WhatsApp credentials are not configured — cannot send.")
        if not settings.WHATSAPP_TEMPLATE_NAME:
            raise CommandError(
                "WHATSAPP_TEMPLATE_NAME is empty — an approved template is required."
            )

        # Meta fetches the template's hero image from a public absolute https
        # URL — grid_image.url is relative (/media/…), so prefix it with
        # PUBLIC_BASE_URL. Without that configured, send the template text-only
        # rather than a broken link Meta can't resolve.
        products = list(campaign.products.prefetch_related("images"))
        hero_image = next((img for p in products if (img := p.grid_image)), None)
        hero_url = None
        if hero_image:
            base = settings.PUBLIC_BASE_URL.rstrip("/")
            if base:
                hero_url = f"{base}/{hero_image.url.lstrip('/')}"
            else:
                logger.warning(
                    "PUBLIC_BASE_URL is unset — sending WhatsApp template '%s' without its "
                    "hero image (a relative media path is not fetchable by Meta).",
                    campaign.name,
                )

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

            ok, error = _whatsapp_template_send(
                client.phone, settings.WHATSAPP_TEMPLATE_NAME, hero_url
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

        campaign.status = Campaign.SENT
        campaign.save(update_fields=["status"])
        self.stdout.write(
            self.style.SUCCESS(f"Done: {sent} sent, {failed} failed, {skipped} already sent.")
        )
