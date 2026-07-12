"""Request counting without touching the database.

Every request increments two cache counters (Redis in prod): a daily total and a
daily per-section counter. Cost per request: two cache ops, zero DB writes.
"""

from django.core.cache import cache
from django.utils import timezone

TTL = 60 * 60 * 24 * 8  # keep 8 days of counters


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
