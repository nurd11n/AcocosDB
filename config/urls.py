from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from apps.core.auth_views import RedirectToSharedLoginMixin, UnifiedLoginView
from apps.core.errors import healthz
from apps.core.otp_views import otp_enroll, otp_enroll_done
from apps.core.views import report_download, root_redirect, set_theme, stats_view
from apps.pos.public_views import receipt_pdf, receipt_public
from apps.reports.views import dashboard, dashboard_export, storage_export, storage_view


# /panel/ shares the one /login/ page — the mixin redirects unauthenticated or
# unauthorized admin hits there instead of Django's separate admin login form.
class _AdminSite(RedirectToSharedLoginMixin, admin.AdminSite):
    def has_permission(self, request):
        """On top of Django's own is_active/is_staff check, require 2FA
        verification when OTP_ENABLED — same as every other surface (see
        apps.core.otp.verified). Mirrors django_otp.admin.OTPAdminSite's own
        has_permission, without pulling in its separate login form/name — this
        site still shares the one /login/ page via RedirectToSharedLoginMixin."""
        return super().has_permission(request) and (
            not settings.OTP_ENABLED or request.user.is_verified()
        )


admin.site.__class__ = _AdminSite

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    # Public — no login, the signed token itself is the access control (see
    # apps.pos.receipts). Short path on purpose: it's what goes into the
    # WhatsApp receipt message.
    path("r/<str:token>/", receipt_public, name="receipt-public"),
    path("r/<str:token>/pdf/", receipt_pdf, name="receipt-pdf"),
    path("login/", UnifiedLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("theme/", set_theme, name="set-theme"),
    path("otp/enroll/", otp_enroll, name="otp-enroll"),
    path("otp/enroll/done/", otp_enroll_done, name="otp-enroll-done"),
    path("pos/", include("apps.pos.urls")),
    path("orders/", include("apps.orders.urls")),
    path("inbox/", include("apps.inbox.urls")),
    path("notes/", include("apps.notes.urls")),
    path("wa/", include("apps.wa.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("dashboard/", dashboard, name="dashboard"),
    path("dashboard/export/", dashboard_export, name="dashboard-export"),
    path("storage/", storage_view, name="storage"),
    path("storage/export/", storage_export, name="storage-export"),
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
