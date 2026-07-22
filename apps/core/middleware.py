"""Request counting without touching the database.

Every request increments two cache counters (Redis in prod): a daily total and a
daily per-section counter. Cost per request: two cache ops, zero DB writes.
"""

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

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
