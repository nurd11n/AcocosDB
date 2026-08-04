"""/inbox/ — the «одно место» unified inbox (CLIENT_BOTS.md §6): every
incoming message from both channels in one stream, a per-client thread
across both channels, reply-from-panel (routes through whichever channel the
client used and triggers the 6h handoff), and inline заявка actions.
"""

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import OuterRef, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.clients.models import Client
from apps.clients.services import client_debt, start_handoff
from apps.core.models import BotMessage
from apps.orders.models import Order
from apps.pos.decorators import pos_view

from .models import Favourite, OrderRequest
from .services import confirm_order_request, decline_order_request, send_to_client

THREAD_LIMIT = 200


def require_inbox_enabled(view):
    """/inbox/ only has real content when at least one bot channel is live —
    with both off (the shipped prod default), the whole app 404s rather than
    showing an always-empty page (CLIENT_BOTS.md-era bots aren't ready yet)."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not (settings.BOTS_ENABLED or settings.WHATSAPP_ENABLED):
            raise Http404
        return view(request, *args, **kwargs)

    return wrapper


def require_can_write(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.has_perm("inbox.add_orderrequest"):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _last_message_per_client():
    """{client_id: BotMessage} — the latest message (either direction) per
    client, one query, used to build the newest-first stream + unanswered
    filter without an N+1."""
    latest_ids = (
        BotMessage.objects.filter(client_id=OuterRef("client_id"))
        .order_by("-created_at")
        .values("id")[:1]
    )
    ids = (
        BotMessage.objects.filter(client__isnull=False)
        .values("client_id")
        .annotate(last_id=Subquery(latest_ids))
        .values_list("last_id", flat=True)
    )
    return list(
        BotMessage.objects.filter(pk__in=ids).select_related("client").order_by("-created_at")
    )


@pos_view
@require_inbox_enabled
def index(request):
    rows = _last_message_per_client()

    channel = request.GET.get("channel", "")
    only_unanswered = request.GET.get("filter") == "unanswered"
    only_requests = request.GET.get("filter") == "requests"
    only_debt = request.GET.get("filter") == "debt"

    if channel:
        rows = [r for r in rows if r.channel == channel]
    if only_unanswered:
        rows = [r for r in rows if r.direction == BotMessage.IN]

    open_request_client_ids = set(
        OrderRequest.objects.filter(status=OrderRequest.NEW).values_list("client_id", flat=True)
    )
    if only_requests:
        rows = [r for r in rows if r.client_id in open_request_client_ids]
    if only_debt:
        rows = [r for r in rows if r.client_id and client_debt(r.client)]

    unanswered_count = sum(1 for r in _last_message_per_client() if r.direction == BotMessage.IN)
    requests_count = len(open_request_client_ids)

    return render(
        request,
        "inbox/index.html",
        {
            "rows": rows,
            "open_request_client_ids": open_request_client_ids,
            "unanswered_count": unanswered_count,
            "requests_count": requests_count,
            "channel": channel,
            "active_filter": request.GET.get("filter", ""),
            "active": "inbox",
            "can_write": request.user.has_perm("inbox.add_orderrequest"),
        },
    )


@pos_view
@require_inbox_enabled
def thread(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    thread_messages = list(
        BotMessage.objects.filter(client=client).order_by("created_at")[:THREAD_LIMIT]
    )
    order_requests = list(client.order_requests.order_by("-created_at")[:10])
    open_orders = list(
        Order.objects.filter(client=client, status__in=Order.OPEN_STATUSES).order_by("due_date")
    )
    last_sale = client.sales.filter(status="confirmed").order_by("-confirmed_at").first()
    favourites = list(Favourite.objects.filter(client=client).select_related("variant__product"))

    return render(
        request,
        "inbox/thread.html",
        {
            "client": client,
            # NEVER "messages" — that key collides with (and silently hides)
            # django.contrib.messages' own context variable, so any
            # messages.error()/messages.success() from a view that redirects
            # back here (reply's send-failure notice, an order-request
            # confirm/decline error) would render nothing at all. Bit us for
            # real: see the reply() view's send-failure message above.
            "thread_messages": thread_messages,
            "order_requests": order_requests,
            "open_orders": open_orders,
            "last_sale": last_sale,
            "favourites": favourites,
            "debt": client_debt(client),
            "active": "inbox",
            "can_write": request.user.has_perm("inbox.add_orderrequest"),
        },
    )


@pos_view
@require_inbox_enabled
@require_can_write
@require_POST
def reply(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    text = request.POST.get("text", "").strip()
    if text:
        sent = send_to_client(client, text)
        # A human just answered — the WhatsApp auto-reply goes quiet so it
        # never talks over the manager who's now handling this conversation.
        start_handoff(client)
        if not sent:
            # send_to_client already wrote the BotMessage row either way (so
            # the thread shows what was TYPED), but silently believing it
            # reached the client when it didn't is the actual failure mode —
            # a manager sees their own reply appear like any other and has
            # no reason to follow up any other way.
            messages.error(
                request,
                _(
                    "Сообщение не доставлено — клиент недоступен ни в Telegram, "
                    "ни в WhatsApp, либо сервис не ответил. Попробуйте связаться "
                    "с клиентом другим способом."
                ),
            )
    return redirect("inbox:thread", client_id=client.pk)


@pos_view
@require_inbox_enabled
@require_can_write
@require_POST
def request_confirm(request, pk):
    order_request = get_object_or_404(OrderRequest, pk=pk)
    due_date = request.POST.get("due_date") or None
    try:
        order = confirm_order_request(order_request, due_date=due_date, user=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("inbox:thread", client_id=order_request.client_id)
    messages.success(request, _("Заявка подтверждена — заказ №%(n)s создан.") % {"n": order.pk})
    return redirect("orders:detail", pk=order.pk)


@pos_view
@require_inbox_enabled
@require_can_write
@require_POST
def request_decline(request, pk):
    order_request = get_object_or_404(OrderRequest, pk=pk)
    reason = request.POST.get("reason", "").strip() or _("Не указана")
    try:
        decline_order_request(order_request, reason, user=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("inbox:thread", client_id=order_request.client_id)
