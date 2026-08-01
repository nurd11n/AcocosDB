"""Unified login for both /pos/ and /panel/ — one form, one page.

Username + password + a one-time code, all in a single POST
(django.contrib.auth's LoginView + django_otp's OTPAuthenticationForm), gated
by settings.OTP_ENABLED (True in prod, False in dev/tests — see
config/settings). Every failure collapses into one generic message so a wrong
username, a wrong password, and a wrong code are all indistinguishable to an
attacker — this now covers the OTP step too (see UnifiedAuthenticationForm),
not just username/password.

/panel/ (admin) shares this exact page — RedirectToSharedLoginMixin sends any
unauthenticated/unauthorized admin hit here instead of rendering Django's
separate admin login form; config.urls._AdminSite.has_permission requires the
same 2FA verification this form produces.

Bootstrap note (see README): a fresh superuser has no device yet and cannot
pass the OTP step. `manage.py addstatictoken <username>` prints a one-time
static token that IS a valid device for this form (django_otp treats a
StaticDevice exactly like a TOTPDevice) — log in with it once, then enroll a
real TOTP device at /otp/enroll/.
"""

from django import forms
from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.signals import user_login_failed
from django.contrib.auth.views import LoginView
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django_otp.forms import OTPAuthenticationForm

# One message for every kind of failure — revealing *which* factor was wrong
# ("no such user", "wrong password", "wrong code") tells an attacker whether
# an account exists and how far their guess got. Two variants, not one: the
# OTP-disabled form has no code field at all, so mentioning "or a code" in
# ITS error would be actively misleading, not just imprecise.
GENERIC_LOGIN_ERROR = _("Неверный логин или пароль.")
GENERIC_LOGIN_ERROR_WITH_OTP = _("Неверный логин, пароль или код.")

# ValidationError codes OTPAuthenticationFormMixin.clean_otp raises for a
# missing/wrong token — see django_otp.forms. Django's own authenticate()
# already fires user_login_failed (which django-axes counts toward its 5-
# failure lockout) for a bad username/password; it does NOT fire again for a
# bad OTP token, since clean_otp() never calls authenticate() a second time —
# it just raises directly. Without re-firing it ourselves here, a correct
# password paired with a wrong code forever would never lock out.
_OTP_FAILURE_CODES = {"token_required", "invalid_token", "not_interactive", "challenge_exception"}


class GenericAuthenticationForm(AuthenticationForm):
    """Used when OTP_ENABLED is False (dev/tests) — plain username+password,
    single generic error. A django-axes lockout raises PermissionDenied (not
    ValidationError), so it still flows to the lockout page untouched."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"autocomplete": "username", "autofocus": True})
        self.fields["password"].widget.attrs.update({"autocomplete": "current-password"})

    def clean(self):
        try:
            return super().clean()
        except forms.ValidationError as exc:
            raise forms.ValidationError(GENERIC_LOGIN_ERROR, code="invalid") from exc


class UnifiedAuthenticationForm(OTPAuthenticationForm):
    """Username + password + one-time code in a single field — no device
    picker. `otp_device`/`otp_challenge` (django_otp's multi-device UI) are
    dropped: OTPAuthenticationFormMixin.clean_otp falls back to
    `match_token()`, which tries the token against every device the user has
    (TOTP, or the static backup codes) when no specific device is chosen —
    the right tradeoff for 2-4 users with one device each, over a dropdown
    nobody needs.

    A bad token is folded into the SAME generic message as a bad password,
    and is made to count toward django-axes' lockout exactly like a bad
    password would (see _OTP_FAILURE_CODES above) — see the module docstring
    for why that isn't automatic."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"autocomplete": "username", "autofocus": True})
        self.fields["password"].widget.attrs.update({"autocomplete": "current-password"})
        self.fields["otp_token"].widget.attrs.update(
            {"autocomplete": "one-time-code", "inputmode": "numeric"}
        )
        del self.fields["otp_device"]
        del self.fields["otp_challenge"]

    def clean(self):
        try:
            return super().clean()
        except forms.ValidationError as exc:
            if getattr(exc, "code", None) in _OTP_FAILURE_CODES and self.get_user() is not None:
                user_login_failed.send(
                    sender=self.__class__,
                    credentials={"username": self.cleaned_data.get("username", "")},
                    request=self.request,
                )
            raise forms.ValidationError(GENERIC_LOGIN_ERROR_WITH_OTP, code="invalid") from exc


class UnifiedLoginView(LoginView):
    template_name = "registration/login.html"

    @property
    def redirect_authenticated_user(self):
        """Django's own LoginView.dispatch() reads this flag alongside a bare
        `request.user.is_authenticated` check — NOT `is_verified()` — to
        decide whether to bounce a visitor away from the login form straight
        to LOGIN_REDIRECT_URL. Left as the plain `= True` this used to be,
        that produces an infinite redirect loop the moment OTP_ENABLED is on:
        apps.core.otp-gated views (pos_view, owner_only, _AdminSite, ...) send
        an authenticated-but-not-yet-verified session to /login/?next=..., and
        Django's own unmodified check immediately sends it straight back to
        that same `next` page (is_authenticated is already True), which
        rejects it again, forever. A session must stay ABLE to see and submit
        the login form (to actually complete the OTP step) until it's fully
        verified, not just authenticated. See tests/test_otp.py's dedicated
        regression test — this is exactly what took the site down."""
        from apps.core.otp import verified

        return verified(self.request.user)

    def get_form_class(self):
        return UnifiedAuthenticationForm if settings.OTP_ENABLED else GenericAuthenticationForm


class RedirectToSharedLoginMixin:
    """Mixed into admin.site's class so /panel/ never renders its own login
    form — everyone authenticates at the one shared /login/ page. Without
    this, Django admin shows a second, separate login form at /panel/login/.

    Two overrides are needed, not one. AdminSite.admin_view() — the wrapper
    every /panel/ view goes through — does NOT consult self.login() or
    settings.LOGIN_URL when permission is denied; it hardcodes a redirect to
    reverse("admin:login") (see django.contrib.admin.sites.AdminSite.admin_view).
    Overriding only login() still works, but every unauthenticated/unauthorized
    /panel/ hit then takes an extra, pointless redirect hop through
    /panel/login/?next=... before landing on /login/?next=.... admin_view()
    is overridden here to skip straight to the shared page in one hop; login()
    stays as a fallback for anyone who lands on /panel/login/ directly (an old
    bookmark, a stale link)."""

    def admin_view(self, view, cacheable=False):
        inner = super().admin_view(view, cacheable=cacheable)

        def wrapped(request, *args, **kwargs):
            if not self.has_permission(request):
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path(), "/login/")
            return inner(request, *args, **kwargs)

        return wrapped

    def login(self, request, extra_context=None):
        next_url = request.GET.get(REDIRECT_FIELD_NAME) or reverse(
            "admin:index", current_app=self.name
        )
        return HttpResponseRedirect(f"/login/?{REDIRECT_FIELD_NAME}={next_url}")
