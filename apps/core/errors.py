"""Custom error handlers + a correlation id for 500s.

Rules (see the Phase 2b spec): no stack traces, no framework/model names, no
PII, one short Russian sentence and one action. The 500 handler renders with
ZERO context and ZERO DB access — the database may be exactly what's down — and
shows a short correlation id that also appears in the server log, so an owner
can find the real error without the page ever leaking it.
"""

import logging
import uuid

from django.http import HttpResponse, HttpResponseServerError
from django.shortcuts import render
from django.template import loader

logger = logging.getLogger("django.request")


def healthz(request):
    """Liveness/readiness probe for Docker + uptime checks. 200 only when BOTH
    the database and the cache backend answer; 503 (with a terse plain-text body
    naming which dependency is down) otherwise. No auth, no templates — just the
    two checks. A Redis outage returns 503 here so the outage is visible, even
    though the app itself degrades gracefully and keeps serving reads."""
    from django.core.cache import cache
    from django.db import connection

    problems = []
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:
        problems.append("db")
    try:
        token = uuid.uuid4().hex
        cache.set("healthz", token, 5)
        if cache.get("healthz") != token:
            problems.append("cache")
    except Exception:
        problems.append("cache")

    if problems:
        return HttpResponse(
            "unhealthy: " + ",".join(problems), status=503, content_type="text/plain"
        )
    return HttpResponse("ok", content_type="text/plain")


class CorrelationIdMiddleware:
    """On an unhandled view exception, mint a short id, log the full traceback
    server-side against it, and stash it on the request so the 500 page can show
    just the id. Logs the path/method only — never names, phones, or ids that
    could be PII (URLs here carry internal row ids, not personal data)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        request.correlation_id = uuid.uuid4().hex[:12]
        logger.exception(
            "Unhandled exception [cid=%s] %s %s",
            request.correlation_id,
            request.method,
            request.path,
        )
        return None  # let Django proceed to handler500


def bad_request(request, exception=None):
    return render(request, "errors/400.html", status=400)


def permission_denied(request, exception=None):
    return render(request, "errors/403.html", status=403)


def page_not_found(request, exception=None):
    return render(request, "errors/404.html", status=404)


def csrf_failure(request, reason=""):
    # A stale/expired CSRF token means the session lapsed — send them to log in.
    return render(request, "errors/403_csrf.html", status=403)


def server_error(request):
    """Deliberately does NOT call render() — render() would attach the request
    and run context processors (DB/session access), and the DB may be the thing
    that's down. loader.render_to_string with no request renders a plain Context:
    no processors, no DB. The template inlines its CSS and hardcodes /pos/."""
    cid = getattr(request, "correlation_id", uuid.uuid4().hex[:12])
    html = loader.render_to_string("errors/500.html", {"correlation_id": cid})
    return HttpResponseServerError(html)
