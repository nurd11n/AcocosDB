from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Bots/WhatsApp/campaigns default ON in dev (and in the test suite, which runs
# under this settings module) so local docker-compose + the existing feature
# test coverage keep exercising those code paths without every dev needing to
# set the flags themselves. base.py's default is False — that's what prod
# ships with. An explicit .env value still wins either way.
BOTS_ENABLED = env.bool("BOTS_ENABLED", default=True)  # noqa: F405
WHATSAPP_ENABLED = env.bool("WHATSAPP_ENABLED", default=True)  # noqa: F405
CAMPAIGNS_ENABLED = env.bool("CAMPAIGNS_ENABLED", default=True)  # noqa: F405

# 2FA defaults OFF in dev/tests (this settings module is also what the test
# suite runs under, per pytest.ini) so `force_login()` and every existing
# password-only test flow keeps working without enrolling a device first.
# base.py's default is True — that's what prod ships with. An explicit .env
# value still wins either way.
OTP_ENABLED = env.bool("OTP_ENABLED", default=False)  # noqa: F405

# Serve static through WhiteNoise in dev too, with NO browser caching, so an
# edited CSS/JS file is picked up immediately in every browser. Without this,
# Safari caches the old stylesheet aggressively and the page looks different
# from Chrome (stale buttons, stale sizes) until you manually empty its cache.
INSTALLED_APPS = ["whitenoise.runserver_nostatic", *INSTALLED_APPS]  # noqa: F405
WHITENOISE_USE_FINDERS = True  # serve straight from the source dirs, no collectstatic
WHITENOISE_AUTOREFRESH = True  # re-check files on each request
WHITENOISE_MAX_AGE = 0  # Cache-Control: max-age=0 — browsers always revalidate
