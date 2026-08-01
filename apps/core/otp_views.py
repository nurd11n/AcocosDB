"""Self-service TOTP enrollment.

Reachable by anyone logged in — whether they got there via a real TOTP device
or the one-time static bootstrap token (see README's fresh-install runbook),
so a brand-new superuser can finish setting up a real device immediately
after using their bootstrap code. Deliberately NOT gated on apps.core.otp's
`verified` check: by the time OTP_ENABLED is True and someone reaches this
page at all, they've already passed a real OTP check to get here (there is
no other way to be logged in) — this only needs plain authentication.

The device is created UNCONFIRMED and only flips to confirmed once the user
proves they can generate a valid token from it — a half-finished enrollment
(QR shown, never scanned) must never count as a working second factor.
"""

import base64
import io

import qrcode
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django_otp.plugins.otp_totp.models import TOTPDevice


def _qr_data_uri(data: str) -> str:
    image = qrcode.make(data)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@login_required
def otp_enroll(request):
    device, _created = TOTPDevice.objects.get_or_create(
        user=request.user, confirmed=False, defaults={"name": "authenticator"}
    )
    error = None
    if request.method == "POST":
        token = (request.POST.get("token") or "").strip()
        if token and device.verify_token(token):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            return redirect("otp-enroll-done")
        error = _("Неверный код. Попробуйте ещё раз.")
    return render(
        request,
        "registration/otp_enroll.html",
        {"qr": _qr_data_uri(device.config_url), "error": error, "active": "otp-enroll"},
    )


@login_required
def otp_enroll_done(request):
    return render(request, "registration/otp_enroll_done.html", {"active": "otp-enroll"})
