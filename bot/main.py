"""ACOCOS Telegram bots — entrypoint.

Runs the staff bot (bot/staff_bot.py) and the client bot (bot/client_bot.py)
concurrently in ONE process, on TWO tokens. They are separate Bot instances
with separate Dispatchers and zero shared handlers — see those modules'
docstrings and tests/test_bots.py for why they must never merge.

Either bot starts independently: a deployment can run with only
TELEGRAM_STAFF_TOKEN or only TELEGRAM_CLIENT_TOKEN set (e.g. rolling out the
client bot later), and that half simply doesn't poll. Both empty is a
configuration error and stops the process, same as before.

BOTS_ENABLED=False (the shipped prod default — bots are not production-ready
yet) makes this process idle forever instead of exiting: the `bot`/`scheduler`
containers run with `restart: unless-stopped` in docker-compose.prod.yml, so
an exit would just restart-loop. Idling keeps the container quiet with zero
Telegram polling — functionally "not started" without the log churn.

Run: python bot/main.py  (or the `bot` service in docker compose)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from aiogram import Bot  # noqa: E402
from django.conf import settings  # noqa: E402

from bot import client_bot, staff_bot  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    if not settings.BOTS_ENABLED:
        logger.warning("BOTS_ENABLED=False — bot process idling, no Telegram polling started.")
        while True:
            await asyncio.sleep(3600)

    # start_polling with no allowed_updates resolves the update types actually
    # used by each dispatcher's handlers (messages only, here) — so polling is
    # already scoped; Telegram won't push update kinds nobody handles.
    runners = []
    if settings.TELEGRAM_STAFF_TOKEN:
        runners.append(staff_bot.dp.start_polling(Bot(settings.TELEGRAM_STAFF_TOKEN)))
        logger.info("Staff bot: starting.")
    else:
        logger.warning("TELEGRAM_STAFF_TOKEN not set — staff bot not started.")

    if settings.TELEGRAM_CLIENT_TOKEN:
        runners.append(client_bot.dp.start_polling(Bot(settings.TELEGRAM_CLIENT_TOKEN)))
        logger.info("Client bot: starting.")
    else:
        logger.warning("TELEGRAM_CLIENT_TOKEN not set — client bot not started.")

    if not runners:
        raise SystemExit(
            "Neither TELEGRAM_STAFF_TOKEN nor TELEGRAM_CLIENT_TOKEN is set in .env — "
            "no bot started."
        )
    await asyncio.gather(*runners)


if __name__ == "__main__":
    asyncio.run(main())
