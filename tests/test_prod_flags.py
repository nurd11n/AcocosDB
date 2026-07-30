"""Production readiness §1 — bots are NOT ready yet, everything else ships.
BOTS_ENABLED / WHATSAPP_ENABLED / CAMPAIGNS_ENABLED must fully gate their
surfaces off (nav, panel, management commands, dashboard, daily report) with
nothing else breaking. dev.py defaults all three True so the rest of the
suite keeps exercising those paths — these tests explicitly flip them False.
"""

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


def test_campaign_admin_hidden_from_superuser_when_campaigns_disabled(
    client, django_user_model, settings
):
    from apps.campaigns.models import Campaign

    settings.CAMPAIGNS_ENABLED = False
    owner = django_user_model.objects.create_superuser("owner_flags", "o@example.com", "x" * 12)
    client.force_login(owner)
    campaign = Campaign.objects.create(name="Off", text_ru="hi")

    assert "Рассылки" not in client.get("/panel/").content.decode()
    resp = client.get(f"/panel/campaigns/campaign/{campaign.pk}/change/")
    assert resp.status_code == 403


def test_send_campaign_refuses_when_campaigns_disabled(settings):
    from django.core.management.base import CommandError

    from apps.campaigns.models import Campaign

    settings.CAMPAIGNS_ENABLED = False
    campaign = Campaign.objects.create(name="Off2", text_ru="hi")
    with pytest.raises(CommandError, match="CAMPAIGNS_ENABLED"):
        call_command("send_campaign", campaign.pk)


def test_inbox_nav_hidden_and_route_404s_when_bots_and_whatsapp_disabled(
    client, django_user_model, settings
):
    settings.BOTS_ENABLED = False
    settings.WHATSAPP_ENABLED = False
    call_command("setup_roles")
    owner = django_user_model.objects.create_superuser("owner_inbox", "o@example.com", "x" * 12)
    client.force_login(owner)

    home = client.get("/pos/today/").content.decode()
    assert "Входящие" not in home

    resp = client.get("/inbox/")
    assert resp.status_code == 404


def test_inbox_reachable_when_only_telegram_bots_enabled(client, django_user_model, settings):
    settings.BOTS_ENABLED = True
    settings.WHATSAPP_ENABLED = False
    owner = django_user_model.objects.create_superuser("owner_inbox2", "o@example.com", "x" * 12)
    client.force_login(owner)
    resp = client.get("/inbox/")
    assert resp.status_code == 200


def test_dashboard_bot_panels_hidden_when_bots_and_whatsapp_disabled(
    client, django_user_model, settings
):
    settings.BOTS_ENABLED = False
    settings.WHATSAPP_ENABLED = False
    owner = django_user_model.objects.create_superuser("owner_dash", "o@example.com", "x" * 12)
    client.force_login(owner)
    body = client.get("/dashboard/").content.decode()
    assert "Охват в Telegram" not in body
    assert "Чаще всего в избранном" not in body


def test_daily_report_skips_telegram_when_bots_disabled(settings, capsys, monkeypatch):
    import requests

    from apps.core.models import BotUser

    settings.BOTS_ENABLED = False
    settings.TELEGRAM_STAFF_TOKEN = "would-be-a-real-token"
    settings.REPORT_RECIPIENTS = []
    BotUser.objects.create(telegram_id=1, name="Owner", receives_reports=True, is_active=True)

    def boom(*a, **k):
        raise AssertionError("must not attempt a Telegram send while BOTS_ENABLED=False")

    monkeypatch.setattr(requests, "post", boom)
    call_command("send_daily_report")
    out = capsys.readouterr().out
    assert "BOTS_ENABLED" in out
