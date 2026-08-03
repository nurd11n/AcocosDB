FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# gettext provides msgfmt, needed to compile locale/*/LC_MESSAGES/*.po -> .mo.
# The rest is WeasyPrint's native rendering stack (cairo/pango/gdk-pixbuf) —
# it renders the receipt PDF from the SAME HTML/CSS as the web page, so the
# two can never drift apart (see apps.pos.receipts).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gettext \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# dev settings on purpose: no SECRET_KEY/REDIS_URL/.env exist at build time,
# and compiling translations touches neither the DB nor prod's strict checks.
RUN DJANGO_SETTINGS_MODULE=config.settings.dev python manage.py compilemessages --locale ru

RUN chmod +x docker/entrypoint.sh

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "-c", "docker/gunicorn.conf.py"]
