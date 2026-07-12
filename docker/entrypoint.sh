#!/bin/sh
set -e

echo "Waiting for database..."
python - << 'PY'
import os, time
import django
django.setup()
from django.db import connections
from django.db.utils import OperationalError
for attempt in range(30):
    try:
        connections["default"].cursor()
        break
    except OperationalError:
        time.sleep(2)
else:
    raise SystemExit("Database never became available")
PY

python manage.py migrate --noinput
python manage.py collectstatic --noinput
exec "$@"
