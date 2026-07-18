"""Every /pos/ view needs the same gate: logged in, and OTP-verified when
OTP_ENABLED. Applied outermost-in so login is checked before OTP verification
(otp_required assumes an authenticated request.user)."""

from django.conf import settings
from django.contrib.auth.decorators import login_required


def pos_view(view):
    if settings.OTP_ENABLED:
        from django_otp.decorators import otp_required

        view = otp_required(view)
    return login_required(view)
