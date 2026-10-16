# ACOCOS CRM

[![Checks](https://github.com/nurd11n/AcocosDB/actions/workflows/privacy.yml/badge.svg)](https://github.com/nurd11n/AcocosDB/actions/workflows/privacy.yml)

A Django application for inventory, point-of-sale transactions, client balances,
and production orders. It combines a responsive staff terminal with an
administration panel, reporting, and scheduled maintenance jobs.

The interface supports Russian-language workflows and multi-currency amounts
in KGS, USD, and RUB. Public examples contain synthetic data.

## Features

- **Inventory:** products, size/color variants, stock movements, product images,
  and catalog imports.
- **Sales and payments:** a POS workflow, discounts, partial payments, returns,
  reversals, client balances, and PDF receipts/statements.
- **Production:** orders, reservations, production batches, contractors, and
  manufacturing expenses.
- **Reporting:** sales and stock dashboards, CSV/XLSX exports, and scheduled
  daily reports.
- **Access control:** Owner, Editor, and Viewer permissions; shared login;
  configurable two-factor authentication; login throttling.
- **Operations:** health checks, scheduled snapshots, encrypted offsite backups,
  and restore-drill tooling.

Telegram, WhatsApp, and campaign integrations are optional and disabled by
default in production. They require separate configuration and readiness review;
core inventory, sales, and reporting work with these integrations disabled.

## Stack

| Area | Technology |
| --- | --- |
| Backend | Python, Django 5.2 LTS |
| Data | PostgreSQL in Docker; SQLite for tests and lightweight local runs |
| Frontend | Django templates, HTMX, JavaScript, installable PWA shell |
| Authentication | Django sessions, django-otp, django-axes |
| Reports | openpyxl, xhtml2pdf, bundled PDF fonts |
| Deployment | Docker Compose, Gunicorn, Caddy, Redis |
| Validation | pytest, Ruff, pip-audit, Gitleaks, GitHub Actions |

## Engineering decisions

Business mutations live in service modules. Stock and client balances are
computed from movements, sales, and payments. Currency conversions capture the
rate used for each transaction so later exchange-rate changes do not rewrite
historical amounts.

The PWA caches static assets only; authenticated pages and business data are
fetched from the server. Report exports preserve numeric/date types and treat
user-entered text as text. Production settings require a strong secret key and
shared cache, and enforce HTTPS and secure cookies.

## Local setup

Requirements: Docker Engine and the Docker Compose plugin.

```bash
cp .env.example .env
# Set local-only values in .env. Never commit this file.
docker compose up -d --build
docker compose exec web python manage.py setup_roles
docker compose exec web python manage.py createsuperuser
```

Open `http://127.0.0.1:8000/`. The development server is bound to localhost.
The staff terminal lives at `/pos/`; the default admin panel is `/panel/`.
Use the [development and deployment guide](docs/DEPLOYMENT.md) for first-login
2FA setup, production configuration, data import, and operational commands.

## Validation

For a local Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip==26.2 -r requirements-dev.txt
pytest -q
python scripts/check_repo_privacy.py --history
pip-audit --progress-spinner off
make checkdeploy
make composecheck
```

The suite covers sales/payment flows, permissions, authentication, reporting,
exports, feature flags, and privacy checks. GitHub Actions runs the tests,
Django deployment checks, dependency audit, history/secret scans, and Compose
validation. Backup integration tests requiring a Docker daemon are opt-in;
see [restore-drill instructions](docs/RESTORE-DRILL.md).

## Project structure

```text
apps/         Domain models, services, views, and management commands
config/       Django settings and URL routing
bot/          Optional messaging integrations
templates/    Staff terminal, admin, and report templates
static/       Styles, JavaScript, fonts, and PWA assets
docker/       Deployment, backup, and restore tooling
tests/        Application and security regression coverage
scripts/      Repository privacy checks
docs/         Deployment guide and synthetic import example
```

## Data privacy

Client databases, uploads, exports, credentials, audit reports, and browser
captures stay outside version control and Docker builds. Deployment values,
report recipients, and personal email addresses belong in the ignored `.env`;
`.env.example` contains placeholders.

Enable the local commit/push checks after cloning:

```bash
git config --local user.email 'YOUR_ID+YOUR_USERNAME@users.noreply.github.com'
git config --local core.hooksPath .githooks
```

Use your own GitHub noreply address. Review anything added to the repository,
including screenshots and examples; automated scans cannot identify every kind
of confidential information.
