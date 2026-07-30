from django.conf import settings


def admin_url(request):
    """Exposes ADMIN_URL to every template — the POS header's «Админпанель»
    link needs it and it's env-configurable, so it can't be hardcoded."""
    return {"ADMIN_URL_PATH": settings.ADMIN_URL}


def feature_flags(request):
    """Bots are not production-ready — templates use these to hide the
    Входящие nav item and dashboard bot panels rather than showing a dead
    link (see .env.example's Feature flags section)."""
    return {
        "BOTS_ENABLED": settings.BOTS_ENABLED,
        "WHATSAPP_ENABLED": settings.WHATSAPP_ENABLED,
        "CAMPAIGNS_ENABLED": settings.CAMPAIGNS_ENABLED,
    }


def theme(request):
    """The user's explicit light/dark choice, read from a cookie (never
    localStorage — see POS-DESIGN.md) so <html data-theme> can be rendered
    server-side with no flash of the wrong theme. Empty string means 'follow
    the OS', handled entirely in tokens.css via prefers-color-scheme."""
    value = request.COOKIES.get("theme", "")
    return {"THEME": value if value in ("light", "dark") else ""}
