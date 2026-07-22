"""Logout must actually end the session — not just redirect the browser away.

Covers: the logout control is a POST (Django's LogoutView is POST-only in this
Django version, so the old GET <a href> link 405'd — it never worked at all),
logout() flushes the session server-side (not just the cookie), a replayed old
session cookie (the Back-button / disk-cache case) is rejected server-side, and
every login-required response carries Cache-Control: no-store so the browser
has nothing to replay from its own cache either.
"""

from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.urls import reverse

from apps.core.permissions import EDITOR


def _login_editor(client, django_user_model, username="logouter"):
    call_command("setup_roles")
    user = django_user_model.objects.create_user(username, password="x" * 12, is_staff=True)
    user.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(user)
    return user


def test_logout_get_is_rejected_not_silently_ignored(client, django_user_model):
    # Documents why the old <a href="{% url 'logout' %}"> nav link never worked:
    # LogoutView only accepts POST in this Django version. A GET must 405, not
    # silently render something that LOOKS like it logged out.
    _login_editor(client, django_user_model)
    resp = client.get("/logout/")
    assert resp.status_code == 405
    # And the session must still be very much alive after the rejected GET.
    assert client.session.get("_auth_user_id") is not None


def test_logout_flushes_server_side_session(client, django_user_model):
    _login_editor(client, django_user_model)
    old_key = client.session.session_key
    assert old_key is not None
    assert Session.objects.filter(session_key=old_key).exists()

    resp = client.post("/logout/")
    assert resp.status_code == 302
    assert resp["Location"] == "/login/"

    # The OLD session key is gone from the store — not merely the cookie unset.
    assert not Session.objects.filter(session_key=old_key).exists()
    assert client.session.get("_auth_user_id") is None


def test_back_button_replay_of_old_session_cookie_is_rejected(client, django_user_model):
    # The concrete "Back button after logout" regression: capture the real
    # session cookie while authenticated, log out, then manually replay that
    # EXACT old cookie value (simulating a cached page / browser history entry
    # that still carries it) and confirm the server refuses it outright.
    _login_editor(client, django_user_model)
    ok = client.get("/pos/today/")
    assert ok.status_code == 200
    old_sessionid = client.cookies["sessionid"].value

    logout_resp = client.post("/logout/")
    assert logout_resp.status_code == 302

    # Replay the pre-logout cookie value verbatim (client.post above already
    # rotated the client's own cookie jar to the new anon session — this forces
    # it back to the stale one, exactly what a Back button / cached tab would
    # send).
    client.cookies["sessionid"] = old_sessionid
    replay = client.get("/pos/today/")
    assert replay.status_code == 302
    assert replay["Location"].startswith("/login/?next=")


def test_unauthenticated_pos_redirects_to_login_with_next(client):
    resp = client.get("/pos/today/")
    assert resp.status_code == 302
    assert resp["Location"] == "/login/?next=/pos/today/"


def test_unauthenticated_panel_redirects_to_login_with_next(client):
    admin_index = reverse("admin:index")
    resp = client.get(admin_index)
    assert resp.status_code == 302
    assert resp["Location"].startswith("/login/?next=")


def test_authenticated_pos_response_has_no_store_headers(client, django_user_model):
    _login_editor(client, django_user_model)
    resp = client.get("/pos/today/")
    assert resp.status_code == 200
    assert "no-store" in resp["Cache-Control"]
    assert resp["Pragma"] == "no-cache"


def test_login_page_itself_is_never_cached(client):
    # Defensive: a shared/kiosk machine must never show a cached login page
    # for the wrong account either.
    resp = client.get("/login/")
    assert "no-store" in resp["Cache-Control"]
