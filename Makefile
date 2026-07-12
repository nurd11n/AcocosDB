.PHONY: dev migrate makemigrations test lint bot backup restore superuser setup-roles

dev:  ## web + db with hot reload
	docker compose up --build

migrate:
	docker compose run --rm web python manage.py migrate

makemigrations:
	docker compose run --rm web python manage.py makemigrations

superuser:
	docker compose run --rm web python manage.py createsuperuser

setup-roles:
	docker compose run --rm web python manage.py setup_roles

test:
	pytest -x

lint:
	ruff check . && ruff format --check .

bot:  ## run the Telegram bot locally (needs TELEGRAM_BOT_TOKEN in .env)
	python bot/main.py

backup:  ## manual pg_dump into ./backups
	docker compose exec db sh -c 'pg_dump -Fc -U $$POSTGRES_USER $$POSTGRES_DB' \
		> backups/manual_$$(date +%Y-%m-%d_%H%M).dump

restore:  ## make restore FILE=backups/xxx.dump — LOCAL db only
	test -n "$(FILE)"
	docker compose exec -T db sh -c 'pg_restore -U $$POSTGRES_USER -d $$POSTGRES_DB --clean --if-exists' < $(FILE)
