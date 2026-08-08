"""Application-layer abuse protection for traffic that arrives over legitimate
HTTPS — the network layer (UFW, Caddy TLS, Caddy-level probe-path blocking,
fail2ban) stops the rest; this stops a flood that presents a valid connection.

Two building blocks, used together everywhere rate limiting matters:

- `cache_is_available()` — a functional probe (write a token, read it back),
  not a try/except around cache calls. CACHES["default"]["OPTIONS"]
  ["IGNORE_EXCEPTIONS"] is True (config/settings/base.py) so the app degrades
  gracefully when Redis is down — but that setting makes a genuine outage
  LOOK exactly like "no requests yet" to a naive `cache.get(key) is None`
  check, which is precisely how the WhatsApp webhook's rate limiter (see
  apps.wa.views, fixed in the same change as this module) used to fail OPEN:
  Redis down -> every count reads as zero -> nothing ever looks
  rate-limited. This probe is the one place that distinguishes "down" from
  "empty" — everything in this module fails CLOSED on top of it (503, never
  silently unlimited).
- `client_ip(request)` — X-Real-IP only, matching AXES_IPWARE_META_PRECEDENCE_ORDER
  (config/settings/base.py) and the Caddyfile's `header_up X-Real-IP
  {remote_host}`, which OVERWRITES anything the client sent. X-Forwarded-For
  is deliberately never read here: its leftmost hop is client-controlled, so
  keying on it would let a flood rotate its way past any limit.
"""

import functools
import logging
import uuid

from django.core.cache import cache
from django.http import HttpResponse
from django.template import loader

logger = logging.getLogger(__name__)

RATE_LIMIT_PROBE_KEY = "ratelimit:probe"

# How long one IP's "already recorded a 429" marker lives. A blocked IP gets
# ONE SecurityEvent row per this window, not one per blocked request — see
# check_rate_limit.
BLOCK_RECORD_WINDOW = 300


def client_ip(request) -> str:
    """The caller's real IP. Read X-Real-IP, which Caddy sets to the true TCP
    peer and OVERWRITES (see Caddyfile) — so it can't be spoofed. Never
    X-Forwarded-For: its leftmost entry is client-controlled (Caddy only
    appends the real IP to that one), so rotating it would defeat any limit
    keyed on it. REMOTE_ADDR is the fallback for a direct hit (dev, tests, no
    proxy in front)."""
    real = request.META.get("HTTP_X_REAL_IP", "").strip()
    return real or request.META.get("REMOTE_ADDR", "unknown")


def cache_is_available() -> bool:
    """A write-then-read roundtrip with a fresh random value, not a bare
    cache.get()/try-except — IGNORE_EXCEPTIONS=True means a down backend
    returns None/no-ops instead of raising, which is indistinguishable from
    a normal cache miss unless you actually verify a round-trip. Mirrors
    apps.core.errors.healthz's own cache check.

    The probe KEY carries the random token too, so each concurrent call
    probes its own key. A single shared key raced badly: request A writes
    tokenA, request B overwrites with tokenB, A reads back tokenB, sees a
    mismatch and concludes the cache is DOWN — so every rate-limited
    endpoint 503s under exactly the concurrent load it exists to survive.
    Measured against a realistic 2ms Redis round trip, 8 concurrent
    callers on one shared key produced 98.5% false "cache down" verdicts;
    per-call keys produced 0%. Never reintroduce a shared probe key."""
    try:
        token = uuid.uuid4().hex
        key = f"{RATE_LIMIT_PROBE_KEY}:{token}"
        cache.set(key, token, 5)
        ok = cache.get(key) == token
        cache.delete(key)  # probes are throwaway — don't accumulate in Redis
        return ok
    except Exception:
        return False


def is_rate_limited(key: str, limit: int, window_seconds: int) -> bool:
    """Fixed-window counter under `key`. add-then-incr so a missing/expired
    key is seeded rather than raising — caller is responsible for checking
    cache_is_available() first (this function alone can't distinguish "down"
    from "empty", same reasoning as the module docstring)."""
    count = cache.get(key)
    if count is not None and count >= limit:
        return True
    if not cache.add(key, 1, window_seconds):
        try:
            cache.incr(key)
        except ValueError:  # key expired between add() and incr()
            cache.set(key, 1, window_seconds)
    return False


def _error_response(template_name: str, status: int, cid: str | None = None) -> HttpResponse:
    html = loader.render_to_string(template_name, {"correlation_id": cid} if cid else {})
    return HttpResponse(html, status=status)


def check_rate_limit(request, prefix: str, limit: int, window_seconds: int) -> HttpResponse | None:
    """The shared check: None means "proceed", anything else is the response
    to return as-is. FAILS CLOSED — a cache outage returns 503, never a free
    pass. Call this directly (like apps.wa.views.webhook does, since it needs
    its own JSON error body) or via the `rate_limit` decorator below for a
    normal Django view."""
    ip = client_ip(request)
    if not cache_is_available():
        logger.error(
            "Rate limiter: cache unavailable, failing closed for %s from %s", prefix, ip
        )
        return _error_response("errors/503.html", 503)
    key = f"rl:{prefix}:{ip}"
    if is_rate_limited(key, limit, window_seconds):
        logger.warning(
            "Rate limit exceeded: %s from %s (limit %s/%ss)", prefix, ip, limit, window_seconds
        )
        # ONE SecurityEvent row per (prefix, IP) per BLOCK_RECORD_WINDOW, not
        # one per blocked request. Writing a row every time inverted the whole
        # point of blocking: a 200-request flood that the limiter had already
        # rejected still cost 200 INSERTs into the primary business database
        # (measured), so the defence amplified DB load under attack instead of
        # shedding it — and grew the table fastest exactly when nobody was
        # watching. cache.add() is atomic, so the first blocked request in the
        # window records and the rest are free. The digest counts DISTINCT IPs
        # (see send_security_digest), so one row per IP is all it ever needed.
        if cache.add(f"rlrec:{prefix}:{ip}", 1, BLOCK_RECORD_WINDOW):
            from .models import SecurityEvent

            SecurityEvent.objects.create(
                event_type=SecurityEvent.RATE_LIMITED, ip=ip, path=request.path[:255]
            )
        return _error_response("errors/429.html", 429)
    return None


def rate_limit(prefix: str, limit: int, window_seconds: int):
    """View decorator form of check_rate_limit. Apply per-endpoint with its
    own sane limit — there is deliberately no single global number; a login
    POST and a live-search keystroke have very different legitimate rates."""

    def decorator(view_func):
        # functools.wraps, not a hand-rolled __name__/__doc__ copy: it also
        # carries __dict__ across, which is where Django stashes view FLAGS
        # like csrf_exempt. Copying only the two dunders silently dropped
        # them, so `@rate_limit` stacked above `@csrf_exempt` would have
        # re-enabled CSRF on a webhook that must not have it — verified, and
        # the kind of thing that breaks quietly the first time someone
        # reorders two decorators.
        @functools.wraps(view_func)
        def wrapped(request, *args, **kwargs):
            blocked = check_rate_limit(request, prefix, limit, window_seconds)
            if blocked is not None:
                return blocked
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
