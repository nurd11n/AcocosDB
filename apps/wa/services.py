"""Outbound WhatsApp sending, factored out of views.py so other apps (the
unified inbox, back-in-stock alerts, заявка outcomes) can push a message to a
client without importing a views module. Same Meta Cloud API call the webhook
already used — one send path, not a second one.
"""

import logging

import requests
from django.conf import settings

from apps.core.models import BotMessage

logger = logging.getLogger(__name__)


def send_whatsapp_text(to: str, body: str, client=None) -> bool:
    """Best-effort send — logs a BotMessage OUT row (linked to `client` when
    known, so /inbox/ can thread it) regardless of whether credentials are
    configured, matching the existing dev-safe "log and skip" behaviour."""
    if not (settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID):
        logger.info("WhatsApp reply skipped (credentials not configured): %s", body)
        BotMessage.objects.create(
            channel=BotMessage.WHATSAPP,
            external_id=to,
            direction=BotMessage.OUT,
            text=body,
            client=client,
        )
        return False
    url = f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    ok = False
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            json={"messaging_product": "whatsapp", "to": to, "text": {"body": body}},
            timeout=10,
        )
        # The network call succeeding is not the same as the message
        # actually being accepted — an expired 24h window, an invalid/
        # unapproved template, or a revoked token all come back as a normal
        # HTTP response, just not a 2xx one. Only a genuine resp.ok means
        # Meta actually queued it (mirrors apps.inbox.services._send_telegram's
        # resp.ok check, which this send used to skip entirely).
        if not resp.ok:
            logger.error(
                "WhatsApp send rejected by Meta (status %s): %s", resp.status_code, resp.text
            )
        ok = resp.ok
    except requests.RequestException:
        logger.exception("Failed to send WhatsApp message")
    BotMessage.objects.create(
        channel=BotMessage.WHATSAPP,
        external_id=to,
        direction=BotMessage.OUT,
        text=body,
        client=client,
    )
    return ok
