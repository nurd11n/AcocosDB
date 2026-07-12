from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.core.views import stats_view

if settings.OTP_ENABLED:
    from django_otp.admin import OTPAdminSite

    admin.site.__class__ = OTPAdminSite

urlpatterns = [
    path(f"{settings.ADMIN_URL}stats/", stats_view, name="stats"),
    path(settings.ADMIN_URL, admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("wa/", include("apps.wa.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
