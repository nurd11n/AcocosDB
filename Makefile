.PHONY: dev prod migrate makemigrations test lint bot backup restore superuser roles report logs

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

lint:
	ruff check . && ruff format --check .

bot:  ## run the Telegram bot locally (needs TELEGRAM_BOT_TOKEN in .env)
	python bot/main.py

report:  ## trigger the daily report manually (add FORMAT=csv for CSV output)
	docker compose run --rm web python manage.py send_daily_report --format $(or $(FORMAT),xlsx)

backup:  ## manual pg_dump into ./backups
	docker compose exec db sh -c 'pg_dump -Fc -U $$POSTGRES_USER $$POSTGRES_DB' \
		> backups/manual_$$(date +%Y-%m-%d_%H%M).dump

restore:  ## make restore FILE=backups/xxx.dump — LOCAL db only
	test -n "$(FILE)"
	docker compose exec -T db sh -c 'pg_restore -U $$POSTGRES_USER -d $$POSTGRES_DB --clean --if-exists' < $(FILE)

logs:  ## tail logs from the prod stack (add SERVICE=web to scope to one service)
	docker compose -f docker-compose.prod.yml logs -f --tail=200 $(SERVICE)
