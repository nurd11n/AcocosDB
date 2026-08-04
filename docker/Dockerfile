FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# gettext provides msgfmt, needed to compile locale/*/LC_MESSAGES/*.po -> .mo.
# Receipt PDFs render via xhtml2pdf (pure Python, see apps.pos.receipts) — no
# native rendering stack needed here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gettext \
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
