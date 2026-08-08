"""Daily Telegram summary of abuse signals — sent to the Owner ONLY when a
threshold is crossed (default: >50 failed logins or >10 distinct IPs hit a
rate limit today). Silent on a normal day: an alert that fires every day
gets muted, and then the day it actually matters gets muted right along
with it — the whole reason this isn't just "send today's counts" daily.

Reads apps.core.models.SecurityEvent, written by:
- apps.core.signals._log_auth_failure (every failed login/OTP token)
- apps.core.middleware.SecurityEventLoggingMiddleware (every 401/403)
- apps.core.ratelimit.check_rate_limit (every 429)

Sends via TELEGRAM_STAFF_TOKEN to DRILL_CHAT_ID — the same Owner chat id
docker/restore_drill.sh already alerts (weekly backup-restore result): same
recipient, same "an ops alert only the Owner needs to see" purpose, no new
setting.
"""

import logging

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import SecurityEvent

logger = logging.getLogger(__name__)

FAILED_LOGIN_THRESHOLD = 50
BLOCKED_IP_THRESHOLD = 10


def _send_telegram_text(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
        if not resp.ok:
            logger.error(
                "Security digest: Telegram rejected the send (status %s): %s",
                resp.status_code,
                resp.text,
            )
        return resp.ok
    except requests.RequestException:
        logger.exception("Security digest: failed to reach Telegram")
        return False


class Command(BaseCommand):
    help = (
        "Send a Telegram summary to the Owner of today's SecurityEvent counts — "
        "ONLY when a threshold is exceeded, silent otherwise."
    )

    def handle(self, *args, **options):
        today = timezone.localdate()
        events_today = SecurityEvent.objects.filter(created_at__date=today)

        failed_logins = events_today.filter(event_type=SecurityEvent.AUTH_FAILURE).count()
        blocked_ips = (
            events_today.filter(event_type=SecurityEvent.RATE_LIMITED)
            .values("ip")
            .distinct()
            .count()
        )
        forbidden = events_today.filter(event_type=SecurityEvent.FORBIDDEN).count()

        over_threshold = (
            failed_logins > FAILED_LOGIN_THRESHOLD or blocked_ips > BLOCKED_IP_THRESHOLD
        )
        if not over_threshold:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Below threshold ({failed_logins} failed logins, {blocked_ips} "
                    f"rate-limited IPs, {forbidden} forbidden) — no alert sent."
                )
            )
            return

        token = settings.TELEGRAM_STAFF_TOKEN
        chat_id = settings.DRILL_CHAT_ID
        if not (token and chat_id):
            self.stdout.write(
                self.style.WARNING(
                    "Threshold exceeded but TELEGRAM_STAFF_TOKEN/DRILL_CHAT_ID is not "
                    "set — alert not sent. "
                    f"({failed_logins} failed logins, {blocked_ips} rate-limited IPs)"
                )
            )
            return

        text = (
            f"⚠️ ACOCOS — повышенная активность за {today.strftime('%d.%m.%Y')}\n"
            f"Неудачных входов: {failed_logins}\n"
            f"IP с превышением лимита запросов: {blocked_ips}\n"
            f"Отказано в доступе (403): {forbidden}\n"
            f"Подробности: /panel/core/securityevent/"
        )
        if _send_telegram_text(token, chat_id, text):
            self.stdout.write(self.style.SUCCESS("Security digest sent."))
        else:
            self.stdout.write(self.style.ERROR("Security digest FAILED to send — see log."))
