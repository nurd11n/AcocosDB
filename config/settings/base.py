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
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="dev-only-insecure-key-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

OTP_ENABLED = env("OTP_ENABLED")  # TOTP 2FA for the admin login — root-mounted, so mandatory
CURRENCY = env("CURRENCY")
REPORT_HOUR = env("REPORT_HOUR")  # HH:MM, TIME_ZONE below — used by the scheduler container

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
    "axes",
    "simple_history",
    "import_export",
    # project apps
    "apps.core",
    "apps.inventory",
    "apps.clients",
    "apps.sales",
    "apps.wa",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "apps.core.middleware.RequestCounterMiddleware",
    "axes.middleware.AxesMiddleware",  # must be last
]

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

# --- Caching: Redis when available, local memory otherwise ---
REDIS_URL = env("REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        }
    }
    # Sessions in Redis -> zero DB hits for session reads.
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"
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

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SESSION_COOKIE_AGE = 60 * 60 * 12  # 12h
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

# --- i18n: English + Russian ---
LANGUAGE_CODE = "en"
LANGUAGES = [("en", _("English")), ("ru", _("Russian"))]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
TIME_ZONE = env("TIME_ZONE")
USE_TZ = True

# --- Static / media ---
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
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
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
WHATSAPP_VERIFY_TOKEN = env("WHATSAPP_VERIFY_TOKEN", default="")
WHATSAPP_TOKEN = env("WHATSAPP_TOKEN", default="")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default="")
WHATSAPP_APP_SECRET = env("WHATSAPP_APP_SECRET", default="")

# --- Admin UI (django-jazzmin) ---
# Business apps first, technical/security apps last. Editor/Viewer groups are never
# granted permissions on core/auth/otp_totp/axes, so those sections simply don't
# render for them — Django's own get_app_list() filtering does the hiding, no
# custom sidebar code needed (see apps/core/permissions.py + setup_roles).
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
        "inventory",
        "inventory.Category",
        "inventory.Product",
        "inventory.ProductVariant",
        "inventory.StockMovement",
        "sales",
        "sales.SaleOrder",
        "sales.Payment",
        "clients",
        "clients.Client",
        "clients.Interaction",
        "core",
        "auth",
        "otp_totp",
        "axes",
    ],
    "icons": {
        "auth": "fas fa-users-cog",
        "auth.User": "fas fa-user",
        "auth.Group": "fas fa-users",
        "inventory.Category": "fas fa-tags",
        "inventory.Product": "fas fa-tshirt",
        "inventory.ProductVariant": "fas fa-boxes",
        "inventory.StockMovement": "fas fa-exchange-alt",
        "sales.SaleOrder": "fas fa-receipt",
        "sales.Payment": "fas fa-money-bill-wave",
        "clients.Client": "fas fa-address-book",
        "clients.Interaction": "fas fa-comments",
        "core.BotUser": "fas fa-robot",
        "core.BotMessage": "fab fa-whatsapp",
    },
    "custom_links": {
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
