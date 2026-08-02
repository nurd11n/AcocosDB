"""Meta WhatsApp Cloud API webhook.

GET  /wa/webhook/  -> verification handshake (hub.challenge)
POST /wa/webhook/  -> receive messages, log them, auto-link/create the Client by
    phone (CRM-linking rule — this is what makes the bot a CRM channel, not just a
    stock lookup tool), and answer with the CLIENT-SAFE reply layer
    (client_replies.build_client_reply — never apps.wa.replies.build_reply, which
    wraps staff-only aggregates and is used ONLY by the internal, allowlisted
    Telegram staff bot). Replies are skipped while the client is in human handoff
    (see apps.clients.services.is_handed_off) and sent only if WHATSAPP_TOKEN and
    WHATSAPP_PHONE_NUMBER_ID are configured.

Unlike the Telegram bot (internal, staff-only, allowlisted), WhatsApp is the
customer-facing channel: any inbound number gets a reply and becomes a Client.
The signature check below verifies the request really came from Meta — it isn't
an allowlist of who may message the business.

WHATSAPP_ENABLED=False (the shipped prod default) makes this whole route 404,
checked before anything else — GET verification included.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.clients.services import (
    is_handed_off,
    log_whatsapp_interaction,
    recent_message_burst,
    start_handoff,
)
from apps.core.models import BotMessage

from .client_replies import build_client_reply
from .services import send_whatsapp_text

logger = logging.getLogger(__name__)

# The webhook is public (Meta calls it, unauthenticated), so it's hardened three
# ways: a payload-size cap (Meta payloads are a few KB), a per-IP rate limit, and
# the HMAC signature check. Meta payloads are small; anything large or fast is not
# Meta.
MAX_WEBHOOK_BYTES = 128 * 1024  # 128 KB
RATE_LIMIT = 300  # requests per source IP...
RATE_WINDOW = 60  # ...per this many seconds


def _client_ip(request) -> str:
    """The caller's real IP. Read X-Real-IP, which Caddy sets to the true TCP peer
    and OVERWRITES (see Caddyfile) — so it can't be spoofed. We deliberately do NOT
    read X-Forwarded-For: its leftmost entry is client-controlled (Caddy only
    appends the real IP), so rotating it would defeat the per-IP limit. REMOTE_ADDR
    is the fallback for a direct hit (dev, no proxy)."""
    real = request.META.get("HTTP_X_REAL_IP", "").strip()
    return real or request.META.get("REMOTE_ADDR", "unknown")


def _rate_limited(ip: str) -> bool:
    """Fixed-window per-IP counter in the cache. add-then-incr so a missing key
    (window rolled over, or Redis restarted) is seeded, never raises."""
    key = f"wa:rl:{ip}"
    count = cache.get(key)
    if count is not None and count >= RATE_LIMIT:
        return True
    if not cache.add(key, 1, RATE_WINDOW):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, RATE_WINDOW)
    return False


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
    # WhatsApp is not production-ready yet (see .env.example's Feature flags
    # section) — a flat 404 regardless of method/credentials means there's no
    # reachable code path here at all while off, not just a disabled reply.
    if not settings.WHATSAPP_ENABLED:
        return HttpResponseNotFound()

    if request.method == "GET":
        # compare_digest, not ==: the verify token is a shared secret, and a
        # plain string compare short-circuits on the first differing byte,
        # leaking its length and prefix to anyone who can time the endpoint.
        # Same discipline as the POST signature check below.
        if settings.WHATSAPP_VERIFY_TOKEN and hmac.compare_digest(
            request.GET.get("hub.verify_token", ""), settings.WHATSAPP_VERIFY_TOKEN
        ):
            return HttpResponse(request.GET.get("hub.challenge", ""))
        return HttpResponseForbidden()

    # Cheap rejects first, before the HMAC and before touching the DB.
    declared = int(request.META.get("CONTENT_LENGTH") or 0)
    if declared > MAX_WEBHOOK_BYTES:
        return HttpResponse(status=413)
    if _rate_limited(_client_ip(request)):
        return HttpResponse(status=429)
    if len(request.body) > MAX_WEBHOOK_BYTES:  # in case Content-Length lied
        return HttpResponse(status=413)
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
        client = log_whatsapp_interaction(wa_id, text)
        BotMessage.objects.create(
            channel=BotMessage.WHATSAPP,
            external_id=wa_id,
            direction=BotMessage.IN,
            text=text,
            client=client,
        )

        # A burst of messages reads as "I need a human", same as the
        # «менеджер» keyword — check BEFORE deciding whether to auto-reply
        # this turn (CLIENT_BOTS.md §4: Phase 4 handoff rules apply here).
        if recent_message_burst(client):
            start_handoff(client)

        if not is_handed_off(client):
            reply_text = build_client_reply(client, text)
            if reply_text:
                send_whatsapp_text(wa_id, reply_text, client=client)

    # Always 200 — Meta retries aggressively on anything else.
    return JsonResponse({"status": "ok"})
