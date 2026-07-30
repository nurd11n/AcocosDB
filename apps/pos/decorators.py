"""Every /pos/ view needs the same gate: logged in. (2FA was removed — a
username + password is the whole login now, see apps.core.auth_views.)"""

from django.contrib.auth.decorators import login_required

pos_view = login_required
