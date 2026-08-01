"""Every /pos/ (and /orders/, /inbox/, /notes/ — they all import this same
decorator) view needs the same gate: logged in AND, when OTP_ENABLED,
two-factor verified. See apps.core.otp.verified and apps.core.auth_views for
the combined username+password+token login form."""

from django.contrib.auth.decorators import user_passes_test

from apps.core.otp import verified

pos_view = user_passes_test(verified)
