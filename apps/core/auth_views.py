"""Unified login for both /pos/ and /panel/ — one form, one page.

Django's LoginView + django_otp's OTPAuthenticationForm combine username,
password, and TOTP token into a single POST. No extra glue is needed for the
admin to recognize the session: django_otp connects to the user_logged_in
signal and persists the verified device the moment django.contrib.auth.login()
fires, which LoginView already calls — see django_otp/__init__.py's
`_handle_auth_login`.
"""

from django import forms
from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

# One message for every kind of failure. Revealing *which* factor was wrong —
# "no such user", "wrong password", "wrong code" — tells an attacker whether an
# account exists and how far their guess got. So username-not-found, bad
# password, missing token, and wrong token all collapse into this single line.
GENERIC_LOGIN_ERROR = _("Неверный логин, пароль или код.")


class _GenericErrorsMixin:
    """Collapse any auth/OTP validation failure into one generic message.

    AuthenticationForm.clean() raises ValidationError on a bad username/password
    (already generic in Django) and django_otp's clean_otp() raises a *specific*
    error for a wrong/missing token — which would leak that the password was
    correct. We catch both and re-raise the single GENERIC_LOGIN_ERROR. A
    django-axes lockout raises PermissionDenied (not ValidationError), so it
    still flows to the lockout page untouched.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"autocomplete": "username", "autofocus": True})
        self.fields["password"].widget.attrs.update({"autocomplete": "current-password"})
        if "otp_token" in self.fields:
            # Lets the browser/phone offer the TOTP code and a numeric keypad.
            self.fields["otp_token"].widget.attrs.update(
                {
                    "autocomplete": "one-time-code",
                    "inputmode": "numeric",
                    "pattern": "[0-9]*",
                }
            )

    def clean(self):
        try:
            return super().clean()
        except forms.ValidationError as exc:
            raise forms.ValidationError(GENERIC_LOGIN_ERROR, code="invalid") from exc


class GenericAuthenticationForm(_GenericErrorsMixin, AuthenticationForm):
    pass


# Built once on first use (import of django_otp is deferred so it isn't pulled
# in when OTP is disabled), then reused — not rebuilt per login request.
_generic_otp_form_class = None


def _otp_form_class():
    global _generic_otp_form_class
    if _generic_otp_form_class is None:
        from django_otp.forms import OTPAuthenticationForm

        class GenericOTPAuthenticationForm(_GenericErrorsMixin, OTPAuthenticationForm):
            pass

        _generic_otp_form_class = GenericOTPAuthenticationForm
    return _generic_otp_form_class


class UnifiedLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_form_class(self):
        return _otp_form_class() if settings.OTP_ENABLED else GenericAuthenticationForm


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
