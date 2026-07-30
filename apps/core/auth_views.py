"""Unified login for both /pos/ and /panel/ — one form, one page.

Plain Django username + password (django.contrib.auth's LoginView). No second
factor: 2FA was intentionally removed — the shop is 2-4 trusted users on their
own devices, and an emailed code was more friction than the threat model
warrants. Every failure collapses into one generic message so a wrong username
and a wrong password are indistinguishable to an attacker.

/panel/ (admin) shares this exact page — RedirectToSharedLoginMixin sends any
unauthenticated/unauthorized admin hit here instead of rendering Django's
separate admin login form.
"""

from django import forms
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

# One message for every kind of failure — revealing *which* factor was wrong
# ("no such user", "wrong password") tells an attacker whether an account
# exists and how far their guess got. So username-not-found and bad password
# both collapse into this single line.
GENERIC_LOGIN_ERROR = _("Неверный логин или пароль.")


class GenericAuthenticationForm(AuthenticationForm):
    """AuthenticationForm with a single generic error and login-friendly widget
    attrs. A django-axes lockout raises PermissionDenied (not ValidationError),
    so it still flows to the lockout page untouched."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"autocomplete": "username", "autofocus": True})
        self.fields["password"].widget.attrs.update({"autocomplete": "current-password"})

    def clean(self):
        try:
            return super().clean()
        except forms.ValidationError as exc:
            raise forms.ValidationError(GENERIC_LOGIN_ERROR, code="invalid") from exc


class UnifiedLoginView(LoginView):
    template_name = "registration/login.html"
    form_class = GenericAuthenticationForm
    redirect_authenticated_user = True


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
