from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from apps.core.auth_views import RedirectToSharedLoginMixin, UnifiedLoginView
from apps.core.errors import healthz
from apps.core.views import report_download, root_redirect, set_theme, stats_view
from apps.reports.views import dashboard

if settings.OTP_ENABLED:
    from django_otp.admin import OTPAdminSite

    class _AdminSite(RedirectToSharedLoginMixin, OTPAdminSite):
        pass
else:

    class _AdminSite(RedirectToSharedLoginMixin, admin.AdminSite):
        pass


admin.site.__class__ = _AdminSite

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("login/", UnifiedLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("theme/", set_theme, name="set-theme"),
    path("pos/", include("apps.pos.urls")),
    path("wa/", include("apps.wa.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("dashboard/", dashboard, name="dashboard"),
    path("stats/", stats_view, name="stats"),
    path("stats/download/", report_download, name="report-download"),
    path(settings.ADMIN_URL, admin.site.urls),
    path("", root_redirect),  # /pos/ is the front door — must stay last
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Custom error handlers — Russian, no stack traces, no PII. The 500 handler is
# deliberately context/DB-free (see apps.core.errors.server_error).
handler400 = "apps.core.errors.bad_request"
handler403 = "apps.core.errors.permission_denied"
handler404 = "apps.core.errors.page_not_found"
handler500 = "apps.core.errors.server_error"
