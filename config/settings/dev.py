from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Serve static through WhiteNoise in dev too, with NO browser caching, so an
# edited CSS/JS file is picked up immediately in every browser. Without this,
# Safari caches the old stylesheet aggressively and the page looks different
# from Chrome (stale buttons, stale sizes) until you manually empty its cache.
INSTALLED_APPS = ["whitenoise.runserver_nostatic", *INSTALLED_APPS]  # noqa: F405
WHITENOISE_USE_FINDERS = True  # serve straight from the source dirs, no collectstatic
WHITENOISE_AUTOREFRESH = True  # re-check files on each request
WHITENOISE_MAX_AGE = 0  # Cache-Control: max-age=0 — browsers always revalidate
