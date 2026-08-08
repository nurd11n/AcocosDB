"""Clear the cached current rates when an ExchangeRate is written, so a refresh
(or a manual admin edit) shows in «≈ сом» figures immediately instead of waiting
out the cache TTL. Also logs every failed login (username/password OR a bad
OTP token — see apps.core.auth_views's own re-firing of this same signal for
the OTP step) to SecurityEvent, for apps.core.management.commands.
send_security_digest and for /panel/'s Система > Security events log.
"""

import logging

from django.contrib.auth.signals import user_login_failed
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import ExchangeRate

logger = logging.getLogger(__name__)


@receiver(post_save, sender=ExchangeRate)
@receiver(post_delete, sender=ExchangeRate)
def _clear_rate_cache(sender, instance, **kwargs):
    cache.delete("rates:current")


@receiver(user_login_failed)
def _log_auth_failure(sender, credentials, request=None, **kwargs):
    from .models import SecurityEvent
    from .ratelimit import client_ip

    ip = client_ip(request) if request is not None else "unknown"
    username = str(credentials.get("username", ""))[:150]
    logger.warning("Failed login: username=%r ip=%s", username, ip)
    SecurityEvent.objects.create(event_type=SecurityEvent.AUTH_FAILURE, ip=ip, username=username)
