from .base import *  # noqa: F401,F403

DEBUG = False

if SECRET_KEY.startswith("dev-only"):  # noqa: F405
    raise RuntimeError("Set a real SECRET_KEY in .env before running production.")

# gunicorn runs multiple worker processes; LocMemCache is per-process, so the
# request counters and cached report totals would silently disagree between
# workers without a shared cache backend.
if not REDIS_URL:  # noqa: F405
    raise RuntimeError(
        "Set REDIS_URL in .env before running production (cache must be shared "
        "across gunicorn workers)."
    )

# HTTPS is terminated by Caddy; Django still enforces secure cookies/headers.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
# The web container's Docker healthcheck probes http://localhost:8000/healthz/
# from INSIDE the container — no Caddy, so no X-Forwarded-Proto=https header.
# Without this exemption SECURE_SSL_REDIRECT would 301 that probe to https on a
# plain-HTTP port, the check would fail, and the container would sit forever
# "unhealthy". /healthz/ is a public liveness probe with no sensitive data, so
# exempting just that path from the redirect is safe; everything else still
# forces https.
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

STORAGES["staticfiles"]["BACKEND"] = (  # noqa: F405
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
