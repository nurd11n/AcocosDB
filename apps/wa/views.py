"""Meta WhatsApp Cloud API webhook.

GET  /wa/webhook/  -> verification handshake (hub.challenge)
POST /wa/webhook/  -> receive messages, log them, auto-link/create the Client by
    phone (CRM-linking rule — this is what makes the bot a CRM channel, not just a
    stock lookup tool), and answer with the shared bilingual reply layer. Replies
    are sent only if WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID are configured.

Unlike the Telegram bot (internal, staff-only, allowlisted), WhatsApp is the
customer-facing channel: any inbound number gets a reply and becomes a Client.
The signature check below verifies the request really came from Meta — it isn't
an allowlist of who may message the business.
"""

import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.clients.services import log_whatsapp_interaction
from apps.core.models import BotMessage

from .replies import build_reply

logger = logging.getLogger(__name__)


def _send_text(to: str, body: str) -> None:
    if not (settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID):
        logger.info("WhatsApp reply skipped (credentials not configured): %s", body)
        return
    url = f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    try:
        requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            json={"messaging_product": "whatsapp", "to": to, "text": {"body": body}},
            timeout=10,
        )
        BotMessage.objects.create(
            channel=BotMessage.WHATSAPP, external_id=to, direction=BotMessage.OUT, text=body
        )
    except requests.RequestException:
        logger.exception("Failed to send WhatsApp message")


def _valid_signature(request) -> bool:
    """Verify Meta's X-Hub-Signature-256 HMAC. Fail closed when the secret is unset."""
    if not settings.WHATSAPP_APP_SECRET:
        logger.warning("WHATSAPP_APP_SECRET is not set — rejecting webhook POST.")
        return False
    header = request.headers.get("X-Hub-Signature-256", "")
    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), request.body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header, f"sha256={expected}")


@csrf_exempt
def webhook(request):
    if request.method == "GET":
        if (
            settings.WHATSAPP_VERIFY_TOKEN
            and request.GET.get("hub.verify_token") == settings.WHATSAPP_VERIFY_TOKEN
        ):
            return HttpResponse(request.GET.get("hub.challenge", ""))
        return HttpResponseForbidden()

    if not _valid_signature(request):
        return HttpResponseForbidden()

    try:
        payload = json.loads(request.body or "{}")
        value = payload["entry"][0]["changes"][0]["value"]
        message = value.get("messages", [None])[0]
    except (KeyError, IndexError, json.JSONDecodeError):
        message = None

    if message and message.get("type") == "text":
        wa_id = message["from"]
        text = message["text"]["body"].strip()
        BotMessage.objects.create(
            channel=BotMessage.WHATSAPP, external_id=wa_id, direction=BotMessage.IN, text=text
        )
        log_whatsapp_interaction(wa_id, text)
        _send_text(wa_id, build_reply(text))

    # Always 200 — Meta retries aggressively on anything else.
    return JsonResponse({"status": "ok"})
