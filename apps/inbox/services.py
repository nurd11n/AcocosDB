"""Заявка lifecycle, favourites, back-in-stock alerts, and the one place both
bots push a message to a client — the logic behind CLIENT_BOTS.md §3.4/3.6
and §6. A заявка never reserves stock or creates production work by itself;
confirming one reuses apps.orders.services.create_order, the SAME path the
/orders/ panel uses — never a second order-creation path.
"""

import logging
import time

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.clients.models import Client
from apps.inventory.services import available_for

from .models import BotContent, Favourite, OrderRequest, OrderRequestItem, StockAlert

logger = logging.getLogger(__name__)

MAX_OPEN_REQUESTS_PER_CLIENT = 5

ORDER_STATUS_RU = {
    "new": "Принят",
    "in_production": "В производстве",
    "ready": "Готов к выдаче",
    "delivered": "Выдан",
    "cancelled": "Отменён",
}


# --- outbound: one send path per channel, shared by both bots and /inbox/ ---


def send_to_client(client: Client, text: str) -> bool:
    """Push a message to a client on whichever channel reaches them —
    Telegram first (free, rich), WhatsApp otherwise. Logs a BotMessage OUT
    row either way so /inbox/ sees it. Returns whether a send was attempted
    at all (False if the client is unreachable on any channel)."""
    from apps.core.models import BotMessage

    if client.telegram_chat_id:
        return _send_telegram(client.telegram_chat_id, text, client=client)
    if client.whatsapp_opted_in and client.phone:
        from apps.wa.services import send_whatsapp_text

        return send_whatsapp_text(client.phone, text, client=client)
    logger.info("Client %s has no reachable channel — message not sent: %s", client.pk, text)
    BotMessage.objects.create(
        channel=BotMessage.TELEGRAM,
        external_id="",
        direction=BotMessage.OUT,
        text=text,
        client=client,
    )
    return False


def _send_telegram(chat_id: int, text: str, client=None) -> bool:
    import requests
    from django.conf import settings

    from apps.core.models import BotMessage

    ok = False
    if settings.TELEGRAM_CLIENT_TOKEN:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_CLIENT_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            ok = resp.ok
        except requests.RequestException:
            logger.exception("Failed to push Telegram message to chat %s", chat_id)
    else:
        logger.info("TELEGRAM_CLIENT_TOKEN not configured — push skipped: %s", text)
    BotMessage.objects.create(
        channel=BotMessage.TELEGRAM,
        external_id=str(chat_id),
        direction=BotMessage.OUT,
        text=text,
        client=client,
    )
    return ok


def notify_staff_new_request(request: OrderRequest) -> None:
    """«Открыть» deep link to the panel, pushed to every active staff BotUser
    over the STAFF token — never the client token, this is internal traffic."""
    import requests
    from django.conf import settings

    from apps.core.models import BotUser

    if not settings.TELEGRAM_STAFF_TOKEN:
        return
    text = (
        f"🆕 Заявка №{request.pk} от {request.client.name} ({request.client.phone})\n"
        f"Откройте /inbox/ в панели, чтобы подтвердить или отклонить."
    )
    for user in BotUser.objects.filter(is_active=True):
        try:
            requests.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_STAFF_TOKEN}/sendMessage",
                data={"chat_id": user.telegram_id, "text": text},
                timeout=15,
            )
        except requests.RequestException:
            logger.exception("Failed to notify staff BotUser %s of заявка %s", user.pk, request.pk)


# --- заявка lifecycle ---


@transaction.atomic
def create_order_request(
    client: Client, items: list[dict], source: str, note: str = "", raw_message: str = ""
) -> OrderRequest:
    """items: [{"variant": ProductVariant, "quantity": int, "note": str}] — may
    be empty for a free-text WhatsApp заявка (raw_message carries the intent
    instead). Rate-limited: past MAX_OPEN_REQUESTS_PER_CLIENT open (новая)
    requests, ask the client to write instead of silently queuing more."""
    open_count = OrderRequest.objects.filter(
        client=client, status__in=OrderRequest.OPEN_STATUSES
    ).count()
    if open_count >= MAX_OPEN_REQUESTS_PER_CLIENT:
        raise ValidationError(
            _("You already have %(n)s open requests — please write to us directly.")
            % {"n": open_count}
        )
    request = OrderRequest.objects.create(
        client=client, source=source, note=note, raw_message=raw_message
    )
    for line in items:
        OrderRequestItem.objects.create(
            request=request,
            variant=line["variant"],
            quantity=line.get("quantity", 1),
            note=line.get("note", ""),
        )
    # After commit: the staff ping is a blocking network send — never hold this
    # atomic block open across it, and never ping about a request that rolled back.
    transaction.on_commit(lambda: notify_staff_new_request(request))
    return request


@transaction.atomic
def confirm_order_request(request: OrderRequest, due_date=None, user=None) -> "Order":  # noqa: F821
    """Подтвердить: converts the заявка into a real apps.orders.Order at the
    variants' current sale_price (staff may adjust the order afterward like
    any other order) — reuses create_order, never a second order-creation
    path. Notifies the client of the outcome."""
    from apps.orders.services import create_order

    locked = OrderRequest.objects.select_for_update().get(pk=request.pk)
    if locked.status != OrderRequest.NEW:
        raise ValidationError(_("This request has already been handled."))
    items = list(locked.items.select_related("variant"))
    if not items:
        raise ValidationError(_("Request has no items — cannot confirm."))

    order = create_order(
        client=locked.client,
        items=[
            {"variant": i.variant, "quantity": i.quantity, "unit_price": i.variant.sale_price}
            for i in items
        ],
        user=user,
        due_date=due_date,
        note=locked.note,
    )
    locked.status = OrderRequest.CONFIRMED
    locked.order = order
    locked.handled_by = user
    locked.handled_at = timezone.now()
    locked.save(update_fields=["status", "order", "handled_by", "handled_at"])

    # After commit — the client notification is a blocking network send; the
    # order + link must be durably saved before we tell the client about it.
    client, pk, order_pk = locked.client, locked.pk, order.pk
    transaction.on_commit(
        lambda: send_to_client(
            client,
            f"Ваша заявка №{pk} подтверждена! Мы создали заказ №{order_pk}. "
            f"Свяжемся с вами по деталям.",
        )
    )
    return order


@transaction.atomic
def decline_order_request(request: OrderRequest, reason: str, user=None) -> OrderRequest:
    locked = OrderRequest.objects.select_for_update().get(pk=request.pk)
    if locked.status != OrderRequest.NEW:
        raise ValidationError(_("This request has already been handled."))
    locked.status = OrderRequest.DECLINED
    locked.decline_reason = reason
    locked.handled_by = user
    locked.handled_at = timezone.now()
    locked.save(update_fields=["status", "decline_reason", "handled_by", "handled_at"])

    # After commit — see confirm_order_request; notify only once the decline is durable.
    client, pk = locked.client, locked.pk
    transaction.on_commit(
        lambda: send_to_client(client, f"К сожалению, заявка №{pk} отклонена. Причина: {reason}")
    )
    return locked


@transaction.atomic
def cancel_order_request(request: OrderRequest) -> OrderRequest:
    """The client cancels their OWN request while it is still новая."""
    locked = OrderRequest.objects.select_for_update().get(pk=request.pk)
    if locked.status != OrderRequest.NEW:
        raise ValidationError(_("This request can no longer be cancelled."))
    locked.status = OrderRequest.CANCELLED
    locked.handled_at = timezone.now()
    locked.save(update_fields=["status", "handled_at"])
    return locked


# --- favourites ---


def toggle_favourite(client: Client, variant) -> bool:
    """Returns True if now favourited, False if it was removed."""
    obj, created = Favourite.objects.get_or_create(client=client, variant=variant)
    if not created:
        obj.delete()
        return False
    return True


def top_favourited(limit: int = 10):
    """«Чаще всего добавляют в избранное» — free demand research for the
    owner, surfaced on the dashboard/panel. [(variant, count), ...]."""
    from apps.inventory.models import ProductVariant

    rows = Favourite.objects.values("variant").annotate(n=Count("id")).order_by("-n")[:limit]
    variants = {v.pk: v for v in ProductVariant.objects.filter(pk__in=[r["variant"] for r in rows])}
    return [(variants[r["variant"]], r["n"]) for r in rows if r["variant"] in variants]


# --- back-in-stock alerts ---


def register_stock_alert(client: Client, variant) -> StockAlert:
    existing = StockAlert.objects.filter(
        client=client, variant=variant, notified_at__isnull=True
    ).first()
    if existing:
        return existing
    return StockAlert.objects.create(client=client, variant=variant)


def notify_back_in_stock(variant) -> int:
    """Called after a PRODUCTION_IN/PURCHASE_IN movement (see
    apps.inventory.services.add_movement). Fires only when availability is
    now actually positive, oldest waiter first, throttled ~1/sec like a
    campaign send. Returns how many were notified."""
    if available_for(variant) <= 0:
        return 0
    waiting = list(
        StockAlert.objects.filter(variant=variant, notified_at__isnull=True)
        .select_related("client")
        .order_by("created_at")
    )
    if not waiting:
        return 0
    text = f"Новинка снова в наличии: {variant}! Успейте заказать."
    notified = 0
    for alert in waiting:
        send_to_client(alert.client, text)
        alert.notified_at = timezone.now()
        alert.save(update_fields=["notified_at"])
        notified += 1
        if notified < len(waiting):
            time.sleep(1)
    return notified


# --- о нас ---


def bot_content(key: str) -> BotContent | None:
    return BotContent.objects.filter(key=key, is_active=True).first()


# --- order-ready push ---


def notify_order_ready(order) -> None:
    """«Ваш заказ №N готов! Ждём вас.» — fired once, from
    apps.orders.services.mark_produced the moment an order auto-advances to
    готов. This single push is the thing that kills «когда будет готово?»
    traffic (CLIENT_BOTS.md §3.5)."""
    send_to_client(order.client, f"Ваш заказ №{order.pk} готов! Ждём вас.")


# --- Telegram reach metric, for the dashboard ---


def telegram_reach() -> dict:
    total = Client.objects.filter(is_active=True).count()
    reachable = Client.objects.filter(is_active=True, telegram_chat_id__isnull=False).count()
    return {"reachable": reachable, "total": total}
