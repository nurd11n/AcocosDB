from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.core.views import report_download, stats_view

if settings.OTP_ENABLED:
    from django_otp.admin import OTPAdminSite

    admin.site.__class__ = OTPAdminSite

urlpatterns = [
    path("wa/", include("apps.wa.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("stats/", stats_view, name="stats"),
    path("stats/download/", report_download, name="report-download"),
    path("", admin.site.urls),  # admin at root — must stay last, it's a catch-all
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
