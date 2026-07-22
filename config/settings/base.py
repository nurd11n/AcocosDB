"""ACOCOS CRM — base settings. Everything configurable comes from .env (see .env.example)."""

from pathlib import Path

import environ
from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    OTP_ENABLED=(bool, False),
    REDIS_URL=(str, ""),
    TIME_ZONE=(str, "Asia/Bishkek"),
    CURRENCY=(str, "KGS"),
    REPORT_HOUR=(str, "21:00"),
    ADMIN_URL=(str, "panel/"),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="dev-only-insecure-key-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

OTP_ENABLED = env("OTP_ENABLED")  # TOTP 2FA on the shared login — mandatory in prod
REPORT_HOUR = env("REPORT_HOUR")  # HH:MM, TIME_ZONE below — used by the scheduler container
ADMIN_URL = env("ADMIN_URL")  # admin is no longer the front door — /pos/ is

# --- Auth routing: /pos/ is the front door, not the admin ---
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/pos/"
LOGOUT_REDIRECT_URL = "/login/"

# --- Currency ---
# Every money-bearing record (ProductVariant prices, SaleOrder total, Payment
# amount) carries its OWN currency and is stored exactly as entered — never
# auto-converted. ACOCOS operates in soms, dollars, and rubles, so the choice
# set is fixed in apps.core.currency, not env-configurable. CURRENCY here is
# only the BASE currency that ExchangeRate rates are quoted against.
CURRENCY = env("CURRENCY")  # base currency, e.g. KGS

INSTALLED_APPS = [
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # security / auditing
    "django_otp",
    "django_otp.plugins.otp_totp",
    # Static (single-use) tokens — used ONCE to bootstrap the first superuser
    # into the admin when OTP_ENABLED=True (a fresh account has no TOTP device,
    # so it could never log in to enroll one). See README's TOTP bootstrap.
    "django_otp.plugins.otp_static",
    "axes",
    "simple_history",
    "import_export",
    # project apps
    "apps.core",
    "apps.inventory",
    "apps.clients",
    "apps.sales",
    "apps.reports",
    "apps.campaigns",
    "apps.pos",
    "apps.notes",
    "apps.wa",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "csp.middleware.CSPMiddleware",  # Content-Security-Policy header
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Early in the list = late in the response chain (outbound order is
    # reversed), so this is one of the LAST things to touch a response before
    # it's sent — nothing downstream can silently strip the no-store header.
    # WhiteNoise above it already short-circuits static/*, so this only ever
    # runs for dynamic pages + /media/ (which it explicitly excludes below).
    "apps.core.middleware.NoStoreMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "apps.core.errors.CorrelationIdMiddleware",  # logs unhandled exceptions with a cid
    "apps.core.middleware.RequestCounterMiddleware",
    "axes.middleware.AxesMiddleware",  # must be last
]

# Custom Russian error pages (handlers live in config/urls.py).
CSRF_FAILURE_VIEW = "apps.core.errors.csrf_failure"
AXES_LOCKOUT_TEMPLATE = "errors/lockout.html"

# --- Content Security Policy (django-csp) ---
# Everything is self-hosted (HTMX/Inter/icons all vendored — no CDN), so the
# policy is strict: script-src 'self' with NO 'unsafe-inline'. Every /pos/ and
# /login/ behaviour lives in an external .js file (confirm.js/pos.js); there are
# no inline <script> blocks or on* handlers to whitelist. Inline STYLE
# attributes are used throughout the layout, so style-src allows 'unsafe-inline'
# (styles can't exfiltrate data the way scripts can). data: images cover the
# icon data-URIs. The Jazzmin admin (third-party, uses inline
# scripts we don't control) is excluded — it's an authenticated staff surface,
# not the public POS the strict policy protects.
CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC = ("'self'",)
CSP_STYLE_SRC = ("'self'", "'unsafe-inline'")
CSP_IMG_SRC = ("'self'", "data:")
CSP_FONT_SRC = ("'self'",)
CSP_CONNECT_SRC = ("'self'",)
CSP_MANIFEST_SRC = ("'self'",)
CSP_WORKER_SRC = ("'self'",)
CSP_FRAME_ANCESTORS = ("'none'",)
CSP_BASE_URI = ("'self'",)
CSP_FORM_ACTION = ("'self'",)
CSP_EXCLUDE_URL_PREFIXES = ("/" + ADMIN_URL,)

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.admin_url",
                "apps.core.context_processors.theme",
            ],
        },
    },
]

# --- Database (Postgres in Docker; sqlite fallback for quick local runs) ---
DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}
# Reuse DB connections instead of opening one per request.
DATABASES["default"]["CONN_MAX_AGE"] = 60
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    # SQLite has one writer lock for the whole file, not real row locking —
    # confirm_sale()'s select_for_update() would otherwise raise "database is
    # locked" under two truly concurrent requests instead of blocking like
    # Postgres does. This only widens the wait window for the sqlite fallback
    # (quick local runs); production Postgres is unaffected.
    DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 20

# --- Caching: Redis when available, local memory otherwise ---
REDIS_URL = env("REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                # Graceful degradation: if Redis is unreachable, cache ops return
                # None (a miss) instead of raising, so the app falls back to the
                # DB and keeps serving reads rather than 500-ing. /healthz/ still
                # reports Redis down so the outage is visible.
                "IGNORE_EXCEPTIONS": True,
            },
        }
    }
    DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = True
    # Sessions read from cache but persist to the DB (cached_db): zero DB hits
    # per request while Redis is up, yet a Redis outage doesn't log everyone out
    # (auth still works off the DB copy). Pure cache-sessions would evaporate.
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --- Auth / lockout ---
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # hours locked out after 5 failed logins
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
# Behind Caddy the real client IP is X-Real-IP (set unspoofably via header_up in
# the Caddyfile). Without this, axes keys on REMOTE_ADDR — always Caddy's own IP —
# so every user shares one IP bucket and the ip dimension of a lockout is
# meaningless. Never read X-Forwarded-For here: its leftmost hop is spoofable.
AXES_IPWARE_META_PRECEDENCE_ORDER = ["HTTP_X_REAL_IP", "REMOTE_ADDR"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Argon2 first (current best practice); PBKDF2 kept after it so any existing
# PBKDF2 hash still verifies and is transparently re-hashed to Argon2 on the
# user's next successful login. Never store or log a plaintext password.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

SESSION_COOKIE_AGE = 60 * 60 * 12  # 12h
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

# Cookie + header hardening that's safe in every environment (the SSL-only bits
# — redirect, HSTS, Secure cookies — live in prod.py so local http still works).
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True  # the token is injected server-side into hx-headers, never read from JS
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- i18n: English + Russian ---
LANGUAGE_CODE = "ru"  # Russian-first — /pos/ is the front door; still switchable to EN
LANGUAGES = [("en", _("English")), ("ru", _("Russian"))]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
TIME_ZONE = env("TIME_ZONE")
USE_TZ = True

# --- Static / media ---
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]  # vendored htmx.min.js lives here — no CDN
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Daily report email (send_daily_report management command) ---
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("EMAIL_HOST_USER", default="acocos@localhost")
REPORT_RECIPIENTS = env.list("REPORT_RECIPIENTS", default=[])

# --- Bots ---
# TWO separate Telegram bots, two tokens — see bot/staff_bot.py and
# bot/client_bot.py. Never share one token between them: the staff bot answers
# revenue/debt/stock queries for allowlisted BotUsers only, the client bot is
# public and must never be able to reach that data even by accident.
TELEGRAM_STAFF_TOKEN = env("TELEGRAM_STAFF_TOKEN", default="")
TELEGRAM_CLIENT_TOKEN = env("TELEGRAM_CLIENT_TOKEN", default="")
WHATSAPP_VERIFY_TOKEN = env("WHATSAPP_VERIFY_TOKEN", default="")
WHATSAPP_TOKEN = env("WHATSAPP_TOKEN", default="")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default="")
WHATSAPP_APP_SECRET = env("WHATSAPP_APP_SECRET", default="")

# --- Admin UI (django-jazzmin) ---
# The sidebar is a workflow, not an alphabetical model dump: Продажи (the sale
# is where everything starts) -> Склад -> Клиенты -> Отчёты -> Система
# (superuser only). Editor/Viewer groups are never granted permissions on
# core/auth/otp_totp/axes, so those sections simply don't render for them —
# Django's own get_app_list() filtering does the hiding, no custom sidebar code
# needed (see apps/core/permissions.py + setup_roles). StockMovement and the
# standalone Payment screen are hidden the same way (has_module_permission).
JAZZMIN_SETTINGS = {
    "site_title": "ACOCOS CRM",
    "site_header": "ACOCOS",
    "site_brand": "ACOCOS CRM",
    "welcome_sign": "ACOCOS CRM",
    "copyright": "ACOCOS",
    # Native topbar dropdowns — no custom template/view needed for either.
    "show_theme_chooser": True,
    "language_chooser": True,
    "order_with_respect_to": [
        "sales",
        "sales.SaleOrder",
        "inventory",
        "inventory.Category",
        "inventory.Product",
        "inventory.ProductVariant",
        "clients",
        "clients.Client",
        "clients.Interaction",
        "campaigns",
        "campaigns.Campaign",
        "reports",
        "reports.DailyReview",
        "core",
        "auth",
        "otp_totp",
        "axes",
    ],
    "icons": {
        "auth": "fas fa-users-cog",
        "auth.User": "fas fa-user",
        "auth.Group": "fas fa-users",
        "sales.SaleOrder": "fas fa-receipt",
        "sales.Payment": "fas fa-money-bill-wave",
        "inventory.Category": "fas fa-tags",
        "inventory.Product": "fas fa-tshirt",
        "inventory.ProductVariant": "fas fa-boxes",
        "inventory.StockMovement": "fas fa-exchange-alt",
        "clients.Client": "fas fa-address-book",
        "clients.Interaction": "fas fa-comments",
        "campaigns.Campaign": "fas fa-bullhorn",
        "reports.DailyReview": "fas fa-clipboard-check",
        "core.BotUser": "fas fa-robot",
        "core.BotMessage": "fab fa-whatsapp",
        "core.ExchangeRate": "fas fa-money-bill-transfer",
    },
    "custom_links": {
        "reports": [
            {"name": "Дашборд", "url": "dashboard", "icon": "fas fa-chart-area"},
            {"name": "Склад", "url": "storage", "icon": "fas fa-warehouse"},
        ],
        "core": [
            {"name": "Statistics", "url": "stats", "icon": "fas fa-chart-line"},
        ],
    },
    "show_ui_builder": False,
    "changeform_format": "horizontal_tabs",
    "related_modal_active": True,
}

JAZZMIN_UI_TWEAKS = {
    "theme": "flatly",
    "navbar": "navbar-white navbar-light",
    "sidebar": "sidebar-light-primary",
    "brand_colour": "navbar-primary",
    "accent": "accent-primary",
    "no_navbar_border": True,
    "sidebar_nav_flat_style": True,
    # Follows OS preference on first visit; the theme chooser lets a user pin
    # light/dark/auto after that (persisted client-side by Jazzmin itself).
    "default_theme_mode": "auto",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
