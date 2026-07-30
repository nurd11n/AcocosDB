"""Nav badge count — one cheap indexed query per page, only for an
authenticated staff user, so «nothing sits unanswered unseen» (CLIENT_BOTS.md
§6) without taxing every /pos/ request with a heavier unanswered-messages scan."""

from django.conf import settings

from .models import OrderRequest


def inbox_badges(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    if not (settings.BOTS_ENABLED or settings.WHATSAPP_ENABLED):
        return {}  # nav item is hidden — no need to query
    return {"inbox_open_requests": OrderRequest.objects.filter(status=OrderRequest.NEW).count()}
