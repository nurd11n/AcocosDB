"""Request counting without touching the database.

Every request increments two cache counters (Redis in prod): a daily total and a
daily per-section counter. Cost per request: two cache ops, zero DB writes.
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

TTL = 60 * 60 * 24 * 8  # keep 8 days of counters


class NoStoreMiddleware:
    """Every dynamic response gets Cache-Control: no-store — this app has no
    public, cacheable page (2-4 trusted users, /pos/ and /panel/ both gated by
    session auth), so nothing is safe to leave in a shared or browser cache.

    This is what actually stops the Back button (or a disk cache) from
    re-displaying an authenticated /pos/, /dashboard/, /storage/, or /notes/
    page after logout: the browser is told never to store the response at all,
    so there's nothing to replay. Static/media assets are excluded — those are
    either hashed+immutable (WhiteNoise sets its own long-lived Cache-Control)
    or public images with nothing to protect.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path
        if path.startswith(settings.STATIC_URL) or path.startswith(settings.MEDIA_URL):
            return response
        if "Cache-Control" not in response:
            response["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response["Pragma"] = "no-cache"
        return response


def _incr(key: str) -> None:
    if not cache.add(key, 1, TTL):
        try:
            cache.incr(key)
        except ValueError:  # key expired between add() and incr()
            cache.set(key, 1, TTL)


class AdminFrameOptionsMiddleware:
    """X_FRAME_OPTIONS = DENY (settings.base) is deliberate for the public
    surface (/pos/, /login/) — a clickjacking iframe from any origin, same-site
    included, must be refused there. But Jazzmin's own "add related object"
    popup (the + next to a select, e.g. adding a Category from the Product
    add form) embeds an admin page inside another admin page via an iframe —
    same-origin, /panel/ framing /panel/. DENY blocks that too, since unlike
    SAMEORIGIN it does not distinguish same-origin from cross-origin — the
    popup opens and shows nothing (see the report this fixes).

    So /panel/ gets SAMEORIGIN instead: it still refuses a clickjacking iframe
    from anyone else's site, it just allows the admin's own same-origin popup.
    Same tradeoff CLAUDE.md already accepts for /panel/'s CSP exclusion —
    Owner-only, behind login, not the public surface DENY protects.

    Must sit AFTER django.middleware.clickjacking.XFrameOptionsMiddleware in
    MIDDLEWARE: response processing runs bottom-to-top, so this sets the
    header first; Django's middleware then sees it's already set and leaves
    it alone (its own source explicitly does this — never overrides an
    existing X-Frame-Options)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/" + settings.ADMIN_URL):
            response["X-Frame-Options"] = "SAMEORIGIN"
        return response


class RequestCounterMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        day = timezone.localdate().isoformat()
        section = request.path.strip("/").split("/")[0] or "root"
        _incr(f"reqcount:{day}:total")
        _incr(f"reqcount:{day}:{section}")
        return response


class SecurityEventLoggingMiddleware:
    """Every 401/403 response gets a log line AND a SecurityEvent row — IP +
    timestamp always, so "what's hitting us" is visible without grepping
    logs, and apps.core.management.commands.send_security_digest has
    something to count. 429s are logged/recorded at the point they're
    produced (apps.core.ratelimit.check_rate_limit) instead of here, since
    that's where the specific rate-limit prefix ("login", "search", ...) is
    known — this middleware only sees a bare status code.

    Deliberately does NOT log 404 — the entire point of the Caddy-level
    probe-path blocking (see docker/Caddyfile) is that scans for /wp-admin,
    /.env etc. never reach Django at all, so a 404 here is far more likely
    to be a genuine typo/dead link than an attack; logging every one would
    bury the signal this middleware exists to surface."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code in (401, 403):
            from .models import SecurityEvent
            from .ratelimit import client_ip

            ip = client_ip(request)
            username = getattr(request.user, "username", "") if hasattr(request, "user") else ""
            logger.warning(
                "Forbidden: %s %s ip=%s user=%s", request.method, request.path, ip, username or "-"
            )
            SecurityEvent.objects.create(
                event_type=SecurityEvent.FORBIDDEN, ip=ip, path=request.path[:255], username=username
            )
        return response
