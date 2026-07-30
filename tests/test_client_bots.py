"""CLIENT_BOTS.md — заявка lifecycle, favourites/back-in-stock, handoff,
Рассылки extensions (frequency cap + quiet hours), and role enforcement for
the unified /inbox/ panel and broadcasts.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.clients.models import Client
from apps.clients.services import (
    is_handed_off,
    recent_message_burst,
    set_marketing_consent,
    start_handoff,
)
from apps.core.models import BotMessage
from apps.core.permissions import EDITOR, VIEWER
from apps.inbox.models import Favourite, OrderRequest, StockAlert
from apps.inbox.services import (
    cancel_order_request,
    confirm_order_request,
    create_order_request,
    decline_order_request,
    notify_order_ready,
    register_stock_alert,
    telegram_reach,
    toggle_favourite,
    top_favourited,
)
from apps.inventory.models import Category, Product, ProductVariant, StockMovement
from apps.inventory.services import add_movement, available_for
from apps.orders.models import Order

pytestmark = pytest.mark.django_db


@pytest.fixture
def variant():
    cat = Category.objects.create(name="Dresses")
    product = Product.objects.create(category=cat, name="Evening Dress")
    return ProductVariant.objects.create(
        product=product,
        sku="EVD-M-RED",
        size="M",
        color="red",
        cost_price=Decimal("1500.00"),
        sale_price=Decimal("3200.00"),
    )


@pytest.fixture
def wa_client():
    return Client.objects.create(first_name="Nurlan", phone="+996700111222", whatsapp_opted_in=True)


# --- заявка lifecycle ---


def test_order_request_never_touches_stock_or_creates_order_until_confirmed(variant, wa_client):
    request = create_order_request(
        wa_client, items=[{"variant": variant, "quantity": 3}], source=OrderRequest.TELEGRAM
    )
    assert request.status == OrderRequest.NEW
    assert request.order is None
    assert not StockMovement.objects.exists()
    assert Order.objects.count() == 0
    assert available_for(variant) == 0  # not reserved either


def test_confirm_order_request_creates_exactly_one_order_and_links_both_ways(variant, wa_client):
    request = create_order_request(
        wa_client, items=[{"variant": variant, "quantity": 2}], source=OrderRequest.WHATSAPP
    )
    order = confirm_order_request(request, due_date=None, user=None)

    request.refresh_from_db()
    assert request.status == OrderRequest.CONFIRMED
    assert request.order_id == order.pk
    assert order.request.pk == request.pk  # linked both ways
    assert Order.objects.count() == 1
    assert order.items.get().quantity == 2


def test_confirming_already_handled_request_raises(variant, wa_client):
    request = create_order_request(
        wa_client, items=[{"variant": variant, "quantity": 1}], source=OrderRequest.TELEGRAM
    )
    confirm_order_request(request)
    with pytest.raises(ValidationError):
        confirm_order_request(request)


def test_decline_order_request_notifies_client_with_reason(
    variant, wa_client, django_capture_on_commit_callbacks
):
    request = create_order_request(
        wa_client, items=[{"variant": variant, "quantity": 1}], source=OrderRequest.TELEGRAM
    )
    # The client notification is deferred to after-commit (network side effect);
    # execute the captured on_commit callbacks so the test sees it.
    with django_capture_on_commit_callbacks(execute=True):
        decline_order_request(request, "Товар снят с продажи")
    request.refresh_from_db()
    assert request.status == OrderRequest.DECLINED
    assert request.decline_reason == "Товар снят с продажи"
    out = BotMessage.objects.filter(client=wa_client, direction=BotMessage.OUT).latest("created_at")
    assert "Товар снят с продажи" in out.text


def test_client_cancels_own_open_request(variant, wa_client):
    request = create_order_request(
        wa_client, items=[{"variant": variant, "quantity": 1}], source=OrderRequest.TELEGRAM
    )
    cancel_order_request(request)
    request.refresh_from_db()
    assert request.status == OrderRequest.CANCELLED


def test_order_request_rate_limit_of_five_open_requests(variant, wa_client):
    for _ in range(5):
        create_order_request(
            wa_client, items=[{"variant": variant, "quantity": 1}], source=OrderRequest.TELEGRAM
        )
    with pytest.raises(ValidationError):
        create_order_request(
            wa_client, items=[{"variant": variant, "quantity": 1}], source=OrderRequest.TELEGRAM
        )


# --- order-ready push ---


def test_order_ready_pushes_notification_once(
    variant, wa_client, django_capture_on_commit_callbacks
):
    from apps.orders.services import create_order, mark_produced

    order = create_order(
        wa_client, items=[{"variant": variant, "quantity": 2, "unit_price": variant.sale_price}]
    )
    item = order.items.get()

    with django_capture_on_commit_callbacks(execute=True):
        mark_produced(item, 1)  # partial — not ready yet, registers no push
    assert not BotMessage.objects.filter(client=wa_client, text__icontains="готов").exists()

    with django_capture_on_commit_callbacks(execute=True):
        mark_produced(item, 1)  # completes it -> ready, push fires exactly once
    order.refresh_from_db()
    assert order.status == Order.READY
    pushes = BotMessage.objects.filter(client=wa_client, text__icontains="готов")
    assert pushes.count() == 1


def test_notify_order_ready_is_idempotent_helper(variant, wa_client):
    from apps.orders.services import create_order

    order = create_order(
        wa_client, items=[{"variant": variant, "quantity": 1, "unit_price": variant.sale_price}]
    )
    notify_order_ready(order)
    assert BotMessage.objects.filter(client=wa_client, text__icontains=f"№{order.pk}").exists()


# --- favourites + back-in-stock ---


def test_favourite_toggle_and_top_favourited_aggregate(variant, wa_client):
    other = Client.objects.create(first_name="Other", phone="+996700333444")

    assert toggle_favourite(wa_client, variant) is True
    assert Favourite.objects.filter(client=wa_client, variant=variant).exists()
    assert toggle_favourite(other, variant) is True

    top = top_favourited(limit=5)
    assert top[0][0] == variant and top[0][1] == 2

    assert toggle_favourite(wa_client, variant) is False  # toggled off
    assert not Favourite.objects.filter(client=wa_client, variant=variant).exists()


def test_back_in_stock_fires_on_production_in_not_on_query(
    variant, wa_client, django_capture_on_commit_callbacks
):
    register_stock_alert(wa_client, variant)
    assert StockAlert.objects.filter(
        client=wa_client, variant=variant, notified_at__isnull=True
    ).exists()

    # A bare read (available_for) must not fire anything — no movement happened.
    available_for(variant)
    assert not BotMessage.objects.filter(client=wa_client, text__icontains="в наличии").exists()

    # The alert is deferred to after-commit (blocking network send); execute the
    # captured callbacks so the test observes the fired notification.
    with django_capture_on_commit_callbacks(execute=True):
        add_movement(variant, StockMovement.PRODUCTION_IN, 5)  # the real intake
    assert BotMessage.objects.filter(client=wa_client, text__icontains="в наличии").exists()

    alert = StockAlert.objects.get(client=wa_client, variant=variant)
    assert alert.notified_at is not None


def test_stock_alert_not_notified_when_still_zero(variant, wa_client):
    register_stock_alert(wa_client, variant)
    # No intake at all yet — still out of stock, no push.
    assert not BotMessage.objects.filter(client=wa_client).exists()


def test_telegram_reach_metric(wa_client):
    reachable = Client.objects.create(
        first_name="Reachable", phone="+996700555666", telegram_chat_id=12345
    )
    reach = telegram_reach()
    assert reach["total"] >= 2
    assert reach["reachable"] == 1
    assert reachable.telegram_chat_id  # sanity


# --- handoff ---


def test_start_and_check_handoff(wa_client):
    assert is_handed_off(wa_client) is False
    start_handoff(wa_client, hours=6)
    wa_client.refresh_from_db()
    assert is_handed_off(wa_client) is True
    assert wa_client.human_handoff_until > timezone.now()


def test_recent_message_burst_detects_three_in_two_minutes(wa_client):
    from apps.clients.models import Interaction

    assert recent_message_burst(wa_client) is False
    for _ in range(3):
        Interaction.objects.create(client=wa_client, kind=Interaction.MESSAGE, note="hi")
    assert recent_message_burst(wa_client) is True


def test_marketing_consent_starts_false_and_is_set_explicitly():
    c = Client.objects.create(first_name="X", phone="+996700777888")
    assert c.marketing_consent is False
    set_marketing_consent(c, True)
    c.refresh_from_db()
    assert c.marketing_consent is True


# --- WhatsApp client-safe replies ---


def test_client_bot_catalog_reply_never_shows_raw_stock_count(variant, wa_client):
    from apps.wa.client_replies import catalog_reply

    add_movement(variant, StockMovement.PRODUCTION_IN, 7)
    text = catalog_reply("Evening Dress")
    assert "7" not in text  # no raw stock number, only "есть"/"нет"
    assert "есть" in text


def test_client_replies_never_leak_aggregate_business_data():
    import inspect

    from apps.wa import client_replies

    source = inspect.getsource(client_replies)
    for leaked in ("total_kgs", "cost_price", "revenue", "profit"):
        assert leaked not in source


def test_whatsapp_stop_unsubscribes_both_consent_fields(wa_client):
    from apps.wa.client_replies import build_client_reply

    set_marketing_consent(wa_client, True)
    build_client_reply(wa_client, "СТОП")
    wa_client.refresh_from_db()
    assert wa_client.marketing_consent is False
    assert wa_client.whatsapp_opted_in is False


def test_whatsapp_manager_keyword_triggers_handoff(wa_client):
    from apps.wa.client_replies import build_client_reply

    build_client_reply(wa_client, "менеджер")
    wa_client.refresh_from_db()
    assert is_handed_off(wa_client) is True


def test_whatsapp_order_status_shows_only_this_clients_orders(variant, wa_client):
    from apps.orders.services import create_order
    from apps.wa.client_replies import build_client_reply

    other = Client.objects.create(first_name="Other2", phone="+996700999000")
    create_order(
        wa_client, items=[{"variant": variant, "quantity": 1, "unit_price": variant.sale_price}]
    )
    create_order(
        other, items=[{"variant": variant, "quantity": 1, "unit_price": variant.sale_price}]
    )

    reply = build_client_reply(wa_client, "мой заказ")
    assert other.name not in reply


def test_whatsapp_intent_words_create_order_request_and_handoff(wa_client):
    from apps.wa.client_replies import build_client_reply

    build_client_reply(wa_client, "Хочу заказать красное платье 46")
    assert OrderRequest.objects.filter(client=wa_client, source=OrderRequest.WHATSAPP).exists()
    wa_client.refresh_from_db()
    assert is_handed_off(wa_client) is True


# --- Рассылки: frequency cap + quiet hours ---


def test_campaign_audience_respects_two_per_week_cap():
    from apps.campaigns.models import Campaign, CampaignRecipient
    from apps.campaigns.services import campaign_audience

    c = Client.objects.create(
        first_name="Capped", phone="+996700123123", telegram_chat_id=555, marketing_consent=True
    )
    campaign = Campaign.objects.create(name="New", text_ru="hi")
    for i in range(2):
        old = Campaign.objects.create(name=f"Old{i}", text_ru="x")
        CampaignRecipient.objects.create(
            campaign=old, client=c, status=CampaignRecipient.SENT, sent_at=timezone.now()
        )
    assert c not in campaign_audience(campaign)


def test_campaign_audience_whatsapp_requires_opted_in_not_just_consent():
    from apps.campaigns.models import Campaign
    from apps.campaigns.services import campaign_audience

    Client.objects.create(
        first_name="TgOnly", phone="+996700456456", telegram_chat_id=1, marketing_consent=True
    )
    wa_opted = Client.objects.create(
        first_name="WaOnly", phone="+996700456457", marketing_consent=True, whatsapp_opted_in=True
    )
    campaign = Campaign.objects.create(name="WA", text_ru="hi", channel=Campaign.WHATSAPP)
    audience = campaign_audience(campaign)
    assert wa_opted in audience
    assert all(c.whatsapp_opted_in for c in audience)


def test_send_campaign_refuses_during_quiet_hours(monkeypatch, settings):
    from apps.campaigns.models import Campaign

    settings.TELEGRAM_CLIENT_TOKEN = "test-token"
    monkeypatch.setattr(
        "apps.campaigns.management.commands.send_campaign._within_quiet_hours", lambda: True
    )
    sent = []
    monkeypatch.setattr(
        "apps.campaigns.management.commands.send_campaign._telegram_send",
        lambda *a: sent.append(1) or (True, None, ""),
    )
    Client.objects.create(
        first_name="Q", phone="+996700654321", telegram_chat_id=901, marketing_consent=True
    )
    campaign = Campaign.objects.create(name="Quiet", text_ru="hi")
    call_command("send_campaign", campaign.pk)
    assert sent == []  # nothing sent — quiet hours


def test_send_campaign_whatsapp_refuses_when_flag_disabled(settings):
    from django.core.management.base import CommandError

    from apps.campaigns.models import Campaign

    settings.WHATSAPP_BROADCAST_ENABLED = False
    Client.objects.create(
        first_name="W", phone="+996700654322", whatsapp_opted_in=True, marketing_consent=True
    )
    campaign = Campaign.objects.create(name="WA-off", text_ru="hi", channel=Campaign.WHATSAPP)
    with pytest.raises(CommandError):
        call_command("send_campaign", campaign.pk, ignore_quiet_hours=True)


# --- roles ---


def test_manager_cannot_access_campaign_admin_gets_403(client, django_user_model):
    from apps.campaigns.models import Campaign

    manager = django_user_model.objects.create_user("manager1", password="pw", is_staff=True)
    manager.groups.add(Group.objects.get_or_create(name=EDITOR)[0])
    call_command("setup_roles")
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)
    campaign = Campaign.objects.create(name="Blocked", text_ru="hi")
    resp = client.post(f"/panel/campaigns/campaign/{campaign.pk}/change/", {})
    assert resp.status_code == 403


def test_viewer_cannot_reply_or_confirm_in_inbox(client, django_user_model, variant):
    call_command("setup_roles")
    viewer = django_user_model.objects.create_user("viewer1", password="pw", is_staff=True)
    viewer.groups.add(Group.objects.get(name=VIEWER))
    client.force_login(viewer)

    c = Client.objects.create(first_name="V", phone="+996700888999")
    resp = client.get(f"/inbox/{c.pk}/")
    assert resp.status_code == 200  # read-only access is fine

    resp = client.post(f"/inbox/{c.pk}/reply/", {"text": "hi"})
    assert resp.status_code == 403

    request = create_order_request(
        c, items=[{"variant": variant, "quantity": 1}], source=OrderRequest.TELEGRAM
    )
    resp = client.post(f"/inbox/requests/{request.pk}/confirm/", {})
    assert resp.status_code == 403


def test_editor_can_reply_and_confirm_in_inbox(client, django_user_model, variant):
    call_command("setup_roles")
    editor = django_user_model.objects.create_user("editor1", password="pw", is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    c = Client.objects.create(first_name="E", phone="+996700111000")
    resp = client.post(f"/inbox/{c.pk}/reply/", {"text": "Здравствуйте!"})
    assert resp.status_code == 302
    assert BotMessage.objects.filter(
        client=c, direction=BotMessage.OUT, text="Здравствуйте!"
    ).exists()

    request = create_order_request(
        c, items=[{"variant": variant, "quantity": 1}], source=OrderRequest.TELEGRAM
    )
    resp = client.post(f"/inbox/requests/{request.pk}/confirm/", {})
    assert resp.status_code == 302
    request.refresh_from_db()
    assert request.status == OrderRequest.CONFIRMED


def test_inbox_reply_triggers_handoff(client, django_user_model):
    call_command("setup_roles")
    editor = django_user_model.objects.create_user("editor2", password="pw", is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    c = Client.objects.create(first_name="H", phone="+996700222000")
    assert is_handed_off(c) is False
    client.post(f"/inbox/{c.pk}/reply/", {"text": "Отвечаю вручную"})
    c.refresh_from_db()
    assert is_handed_off(c) is True
