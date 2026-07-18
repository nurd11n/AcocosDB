.PHONY: dev prod migrate makemigrations test lint bot backup restore superuser roles report rates campaign logs

dev:  ## web + db with hot reload
	docker compose up --build

prod:  ## full production stack (web, db, redis, bot, scheduler, caddy, backup)
	docker compose -f docker-compose.prod.yml up --build -d

migrate:
	docker compose run --rm web python manage.py migrate

makemigrations:
	docker compose run --rm web python manage.py makemigrations

superuser:
	docker compose run --rm web python manage.py createsuperuser

roles:
	docker compose run --rm web python manage.py setup_roles

test:
	pytest -x
	$(MAKE) checkdeploy

checkdeploy:  ## run Django's deployment security checklist against prod settings
	DJANGO_SETTINGS_MODULE=config.settings.prod \
		SECRET_KEY="$${SECRET_KEY:-$$(python -c 'import secrets;print(secrets.token_urlsafe(64))')}" \
		REDIS_URL="$${REDIS_URL:-redis://localhost:6379/0}" \
		DATABASE_URL="$${DATABASE_URL:-sqlite:////tmp/acocos_check.sqlite3}" \
		ALLOWED_HOSTS="$${ALLOWED_HOSTS:-acocos.example.com}" \
		python manage.py check --deploy

lint:
	ruff check . && ruff format --check .

bot:  ## run the Telegram bot locally (needs TELEGRAM_BOT_TOKEN in .env)
	python bot/main.py

report:  ## trigger the daily report manually (add FORMAT=csv for CSV output)
	docker compose run --rm web python manage.py send_daily_report --format $(or $(FORMAT),xlsx)

rates:  ## fetch today's FX rates from NBKR
	docker compose run --rm web python manage.py fetch_rates

campaign:  ## send a campaign — make campaign ID=3 (add DRY=1 to preview only)
	docker compose run --rm web python manage.py send_campaign $(ID) $(if $(DRY),--dry-run,)

backup:  ## manual pg_dump into ./backups
	docker compose exec db sh -c 'pg_dump -Fc -U $$POSTGRES_USER $$POSTGRES_DB' \
		> backups/manual_$$(date +%Y-%m-%d_%H%M).dump

restore:  ## make restore FILE=backups/xxx.dump — LOCAL scratch db only, never prod
	test -n "$(FILE)"
	@case "$${DJANGO_SETTINGS_MODULE:-}" in \
		*prod*) echo "REFUSED: DJANGO_SETTINGS_MODULE looks like prod. Restore only to a local scratch DB."; exit 1;; \
	esac
	@echo "Restoring $(FILE) into the LOCAL (docker-compose.yml) database..."
	docker compose exec -T db sh -c 'pg_restore -U $$POSTGRES_USER -d $$POSTGRES_DB --clean --if-exists' < $(FILE)

logs:  ## tail logs from the prod stack (add SERVICE=web to scope to one service)
	docker compose -f docker-compose.prod.yml logs -f --tail=200 $(SERVICE)
