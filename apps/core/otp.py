"""The one predicate every protected surface in this project gates on.

Two-factor is enforced past the login form itself — a session that somehow
ended up authenticated without going through it (an old pre-2FA session, a
future login path someone adds without thinking) must not get a free pass
just because `request.user.is_authenticated` is True. Every gate in this
project (apps.pos.decorators.pos_view, apps.reports.views.owner_only,
apps.core.views._superuser_required, config.urls._AdminSite.has_permission)
calls `verified(request.user)`, never `is_authenticated` alone.

`OTP_ENABLED=False` (dev/tests) makes this behave exactly like the old
is_authenticated-only check, so the existing test suite's force_login() /
password-only login flows are unaffected.
"""

from django.conf import settings


def verified(user) -> bool:
    if not user.is_authenticated:
        return False
    if not settings.OTP_ENABLED:
        return True
    return user.is_verified()
