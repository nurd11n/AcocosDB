"""Settings the test suite runs under — deterministic on every machine.

WHY THIS EXISTS: the suite used to run under config.settings.dev, which reads
feature flags and money thresholds from .env. A developer whose .env mirrors
production (BOTS_ENABLED=False, WHATSAPP_ENABLED=False, CAMPAIGNS_ENABLED=False
— the shipped defaults) got 11 red tests on a clean checkout, because the
bot/WhatsApp/campaign tests exercise surfaces those flags gate off. That makes
a green suite a property of an untracked local file rather than of the code,
and it cuts both ways: a genuinely broken feature could be masked by flipping
an env var.

So everything a test asserts against is PINNED here and deliberately does NOT
consult the environment:

- the three feature flags, ON, so the feature tests reach the code they cover
  (tests/test_prod_flags.py still sets them False explicitly where it asserts
  the off path — that stays the one place the gating itself is verified);
- OTP_ENABLED off, so force_login() works without enrolling a device
  (tests/test_otp.py turns it on explicitly for the 2FA assertions);
- the money thresholds, because the change/rounding/large-payment tests assert
  exact сом figures that only hold at these values.
"""

from decimal import Decimal

from .dev import *  # noqa: F401,F403

BOTS_ENABLED = True
WHATSAPP_ENABLED = True
CAMPAIGNS_ENABLED = True

OTP_ENABLED = False

# Money knobs the suite asserts exact figures against — see the change/rounding
# tests in tests/test_flows.py. base.py reads each of these from .env.
CURRENCY = "KGS"
PAYMENT_ROUNDING_TOLERANCE = Decimal("1.00")
CHANGE_ROUNDING_STEP = Decimal("1.00")
LARGE_PAYMENT_THRESHOLD_KGS = 10000
CHANGE_CONFIRM_THRESHOLD_KGS = 5000
MANUAL_RATE_DEVIATION_WARN_PCT = Decimal("0.05")

# Never let a real .env DATABASE_URL (a docker "db" host, or worse, a live
# database) capture the test run — tests always get their own local sqlite.
DATABASES = {  # noqa: F405
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",  # noqa: F405
        "OPTIONS": {"timeout": 20},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "TIME_ZONE": None,
    }
}

# LocMem, so a stale/unreachable REDIS_URL in .env can neither fail the run nor
# leak cached state between tests via a shared Redis db.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "acocos-tests",
    }
}
