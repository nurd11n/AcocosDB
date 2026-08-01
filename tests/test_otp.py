"""2FA (S0 of the security remediation): every protected surface requires
`request.user.is_verified()` when settings.OTP_ENABLED, not just
`is_authenticated` — see apps.core.otp.verified.

The rest of the suite runs with OTP_ENABLED=False (config/settings/dev.py's
default, per pytest.ini), which is itself the "OTP_ENABLED=False leaves dev
unaffected" guarantee: every other test file's force_login()-based flow keeps
passing unmodified. These tests explicitly flip OTP_ENABLED=True to exercise
the gate itself, and confirm the flag genuinely toggles it back off too.
"""

from django.contrib.auth.models import Group
from django.core.management import call_command
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken

from apps.core.permissions import EDITOR


def _give_static_token(user, token="12345678"):
    device = StaticDevice.objects.create(user=user, name="backup", confirmed=True)
    StaticToken.objects.create(device=device, token=token)
    return device


def test_otp_disabled_leaves_dev_unaffected(client, django_user_model, settings):
    """The default (dev/tests): force_login() reaches every surface exactly
    like before this whole change — no token, no device, nothing new."""
    settings.OTP_ENABLED = False
    owner = django_user_model.objects.create_superuser("otp_off_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    assert client.get("/pos/").status_code in (200, 302)  # reaches the app, not bounced to /login/
    assert client.get("/panel/").status_code == 200
    assert client.get("/dashboard/").status_code == 200


def test_unverified_session_is_rejected_on_every_protected_surface(
    client, django_user_model, settings
):
    """An authenticated session that never went through the OTP step (exactly
    what force_login() produces) must be bounced to /login/ on EVERY surface
    once OTP_ENABLED — /pos/, /panel/, /dashboard/, and a report download —
    not just the admin. This is the core claim S0 exists to fix."""
    settings.OTP_ENABLED = True
    call_command("setup_roles")
    owner = django_user_model.objects.create_superuser("unverified_owner", "o@e.com", "x" * 12)
    client.force_login(owner)  # authenticated, but no otp_device in this session

    for url in ("/pos/", "/panel/", "/dashboard/", "/stats/", "/stats/download/?format=csv"):
        resp = client.get(url)
        assert resp.status_code == 302, f"{url} should redirect to /login/, got {resp.status_code}"
        assert resp.url.startswith("/login/"), f"{url} redirected to {resp.url!r}, not /login/"

    # An Editor (non-superuser) session is rejected the same way, before role
    # checks even get a chance to run.
    editor = django_user_model.objects.create_user(
        "unverified_editor", password="x" * 12, is_staff=True
    )
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)
    resp = client.get("/pos/")
    assert resp.status_code == 302
    assert resp.url.startswith("/login/")


def test_login_form_shows_no_otp_field_when_disabled(client, django_user_model, settings):
    settings.OTP_ENABLED = False
    resp = client.get("/login/")
    assert 'name="otp_token"' not in resp.content.decode()


def test_login_form_shows_otp_field_when_enabled(client, settings):
    settings.OTP_ENABLED = True
    resp = client.get("/login/")
    assert 'name="otp_token"' in resp.content.decode()


def test_static_token_logs_in_once_and_is_consumed(client, django_user_model, settings):
    """A static backup token is a real, valid device — logging in with it
    succeeds exactly once; reusing the same code afterward fails because
    django_otp deletes it on first successful use."""
    settings.OTP_ENABLED = True
    user = django_user_model.objects.create_superuser("static_owner", "o@e.com", "x" * 12)
    _give_static_token(user, token="onetime1")

    resp = client.post(
        "/login/", {"username": "static_owner", "password": "x" * 12, "otp_token": "onetime1"}
    )
    assert resp.status_code == 302 and resp.url == "/pos/"
    assert not StaticToken.objects.filter(token="onetime1").exists()  # consumed

    client.post("/logout/")
    resp = client.post(
        "/login/", {"username": "static_owner", "password": "x" * 12, "otp_token": "onetime1"}
    )
    assert resp.status_code == 200  # form redisplayed — rejected, token already spent
    assert "otp_token" in resp.context["form"].data or resp.context["form"].errors


def test_wrong_token_counts_toward_the_axes_lockout(client, django_user_model, settings):
    """A wrong password already counts toward django-axes' 5-failure lockout
    via Django's own authenticate() signal. A wrong OTP token must count the
    SAME way — OTPAuthenticationFormMixin.clean_otp() does NOT re-fire that
    signal on its own (it raises ValidationError directly), so without the
    explicit re-fire in UnifiedAuthenticationForm.clean(), a correct password
    paired with a wrong code would never be rate-limited at all."""
    from axes.models import AccessAttempt

    settings.OTP_ENABLED = True
    user = django_user_model.objects.create_superuser("axes_owner", "o@e.com", "x" * 12)
    _give_static_token(user, token="realcode1")

    assert not AccessAttempt.objects.exists()
    resp = client.post(
        "/login/", {"username": "axes_owner", "password": "x" * 12, "otp_token": "wrong-code"}
    )
    assert resp.status_code == 200  # rejected
    attempt = AccessAttempt.objects.get(username="axes_owner")
    assert attempt.failures_since_start == 1


def test_wrong_token_and_right_password_show_the_same_generic_message(
    client, django_user_model, settings
):
    """A wrong OTP token must not reveal that the password was actually
    correct — same generic message as a wrong password/username."""
    settings.OTP_ENABLED = True
    django_user_model.objects.create_superuser("generic_owner", "o@e.com", "x" * 12)

    resp_bad_password = client.post(
        "/login/", {"username": "generic_owner", "password": "wrong", "otp_token": "123456"}
    )
    resp_bad_token = client.post(
        "/login/", {"username": "generic_owner", "password": "x" * 12, "otp_token": "wrong-code"}
    )
    msg_password = resp_bad_password.context["form"].non_field_errors()[0]
    msg_token = resp_bad_token.context["form"].non_field_errors()[0]
    assert msg_password == msg_token


def test_admin_site_has_permission_requires_verification(client, django_user_model, settings):
    """config.urls._AdminSite.has_permission — the actual mechanism gating
    /panel/ — directly, not just through the client."""
    from django.contrib import admin
    from django.test import RequestFactory

    settings.OTP_ENABLED = True
    owner = django_user_model.objects.create_superuser("admin_perm_owner", "o@e.com", "x" * 12)
    request = RequestFactory().get("/panel/")
    request.user = owner
    request.user.otp_device = None  # unverified, exactly what OTPMiddleware sets pre-verification
    request.user.is_verified = lambda: False
    assert admin.site.has_permission(request) is False

    request.user.is_verified = lambda: True
    assert admin.site.has_permission(request) is True
