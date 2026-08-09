"""Bulk import of pre-system client purchase history from an .xlsx
spreadsheet — the companion to import_opening_balances: that command
transfers what a client OWES from before go-live, this one transfers what
she BOUGHT, so «История покупок» on the client page reads like a real
history instead of starting blank on day one.

Expected header row (exact Russian column names, any order, row 1):
  клиент или телефон | дата | описание | сумма | валюта | заметка

- «клиент или телефон» matching is IDENTICAL to import_opening_balances:
  phone first (digit-exact, format-tolerant — see
  apps.clients.services.find_client_by_phone), then exact first-name if the
  cell doesn't look like a phone number. A miss — or an ambiguous name — is
  REPORTED, never guessed; this command never creates a client.
- «описание» is free text ("3 вечерних платья L, 2 карнавальных костюма")
  — NOT matched against today's catalog. Old items may not exist as tracked
  SKUs at all, and this import must never be able to touch stock.
- Every row is validated FIRST; any bad row aborts the WHOLE import with a
  Russian, row-numbered error list — nothing is written on a partial
  failure, same discipline as import_catalog/import_opening_balances.
- Idempotent by CONTENT, not by upsert: HistoricalPurchase's own
  UniqueConstraint is the full row (client, date, description, amount,
  currency), so an exact re-run of the same file is a no-op. This is
  deliberately different from import_opening_balances' upsert-by-key — a
  purchase is an EVENT, and the same client can have several genuinely
  different purchases on the same day, so there is no smaller natural key
  to upsert on. Correcting a typo'd row afterward is done in the admin, not
  by re-running the file with different text (see HistoricalPurchase's own
  docstring).
- The whole import is ONE atomic transaction.
- A preview (clients affected, total per currency) is shown before writing,
  and requires explicit confirmation. --dry-run only validates, --yes skips
  the interactive prompt (CI/scripted runs).

Run: python manage.py import_purchase_history purchases.xlsx [--dry-run] [--yes] [--user <username>]
"""

from decimal import Decimal, InvalidOperation

import openpyxl
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.clients.models import Client, HistoricalPurchase
from apps.clients.services import find_client_by_phone
from apps.core.currency import CURRENCY_CODES

from ._import_helpers import clean as _clean
from ._import_helpers import looks_like_phone as _looks_like_phone
from ._import_helpers import parse_date as _parse_date

REQUIRED_HEADERS = ["клиент или телефон", "дата", "описание", "сумма", "валюта", "заметка"]


def _decimal(value):
    """Same discipline as import_catalog._decimal: always via str(), never
    Decimal(float), so no binary rounding error sneaks into stored money."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        return None


class Command(BaseCommand):
    help = "Bulk-import pre-system client purchase history from an .xlsx file."

    def add_arguments(self, parser):
        parser.add_argument("xlsx_path")
        parser.add_argument("--dry-run", action="store_true", help="Validate only, write nothing.")
        parser.add_argument(
            "--yes", action="store_true", help="Skip the confirmation prompt (CI/scripted runs)."
        )
        parser.add_argument(
            "--user", default=None, help="Username to record as created_by on new rows."
        )

    def handle(self, *args, **options):
        path = options["xlsx_path"]
        try:
            wb = openpyxl.load_workbook(path, data_only=True)
        except FileNotFoundError as exc:
            raise CommandError(f"Файл не найден: {path}") from exc
        except Exception as exc:  # corrupt/non-xlsx file
            raise CommandError(f"Не удалось открыть файл: {exc}") from exc

        creator = None
        if options["user"]:
            try:
                creator = get_user_model().objects.get(username=options["user"])
            except get_user_model().DoesNotExist as exc:
                raise CommandError(f"Пользователь «{options['user']}» не найден.") from exc

        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise CommandError("Файл пуст.")

        header_row = [_clean(c).lower() for c in rows[0]]
        col_index = {
            name: header_row.index(name) for name in REQUIRED_HEADERS if name in header_row
        }
        missing_headers = [h for h in REQUIRED_HEADERS if h not in col_index]
        if missing_headers:
            raise CommandError("В файле отсутствуют колонки: " + ", ".join(missing_headers))

        errors: list[str] = []
        parsed_rows: list[dict] = []
        seen_keys: set[tuple] = set()

        for i, raw_row in enumerate(rows[1:], start=2):
            if raw_row is None or all(c is None or _clean(c) == "" for c in raw_row):
                continue  # a trailing/blank row — skip silently, not an error

            def cell(name):
                idx = col_index[name]
                return raw_row[idx] if idx < len(raw_row) else None

            who_raw = _clean(cell("клиент или телефон"))
            purchase_date = _parse_date(cell("дата"))
            description = _clean(cell("описание"))
            amount = _decimal(cell("сумма"))
            currency = _clean(cell("валюта")).upper()
            note = _clean(cell("заметка"))

            row_errors = []
            client = None
            if not who_raw:
                row_errors.append("пусто «клиент или телефон»")
            elif _looks_like_phone(who_raw):
                client = find_client_by_phone(who_raw)
                if client is None:
                    row_errors.append(f"клиент с телефоном «{who_raw}» не найден")
            else:
                matches = list(Client.objects.filter(first_name__iexact=who_raw))
                if not matches:
                    row_errors.append(f"клиент «{who_raw}» не найден")
                elif len(matches) > 1:
                    row_errors.append(
                        f"имя «{who_raw}» соответствует нескольким клиентам — укажите телефон"
                    )
                else:
                    client = matches[0]

            if purchase_date is None:
                row_errors.append("дата не распознана (ожидается ДД.ММ.ГГГГ)")
            if not description:
                row_errors.append("пустое «описание»")
            if amount is None or amount <= 0:
                row_errors.append("сумма должна быть числом больше 0")
            if currency not in CURRENCY_CODES:
                row_errors.append(
                    f"неизвестная валюта «{currency}» (ожидается {', '.join(CURRENCY_CODES)})"
                )

            if (
                client is not None
                and purchase_date is not None
                and description
                and amount is not None
                and currency in CURRENCY_CODES
            ):
                key = (client.pk, purchase_date, description, amount, currency)
                if key in seen_keys:
                    row_errors.append(
                        "дублирует другую строку этого же файла (тот же клиент, дата, описание, сумма и валюта)"
                    )
                else:
                    seen_keys.add(key)

            if row_errors:
                errors.append(f"Строка {i}: " + "; ".join(row_errors) + ".")
                continue

            parsed_rows.append(
                {
                    "row": i,
                    "client": client,
                    "purchase_date": purchase_date,
                    "description": description,
                    "amount": amount,
                    "currency": currency,
                    "note": note,
                }
            )

        if errors:
            self.stderr.write(self.style.ERROR(f"{len(errors)} ошибок — ничего не записано:"))
            for e in errors:
                self.stderr.write(f"  {e}")
            raise CommandError("Импорт остановлен из-за ошибок в файле.")

        if not parsed_rows:
            self.stdout.write(self.style.WARNING("Нет строк для импорта."))
            return

        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(f"Проверка пройдена: {len(parsed_rows)} строк(и), ошибок нет.")
            )
            return

        totals: dict[str, Decimal] = {}
        client_ids = set()
        for data in parsed_rows:
            totals[data["currency"]] = totals.get(data["currency"], Decimal("0")) + data["amount"]
            client_ids.add(data["client"].pk)

        self.stdout.write(f"Будет затронуто клиентов: {len(client_ids)}")
        for currency, total in totals.items():
            self.stdout.write(f"  {currency}: {total}")

        if not options["yes"]:
            answer = input("Продолжить и записать эти покупки? [y/N]: ").strip().lower()
            if answer not in ("y", "yes", "д", "да"):
                self.stdout.write(self.style.WARNING("Отменено — ничего не записано."))
                return

        created = skipped = 0
        with transaction.atomic():
            for data in parsed_rows:
                _obj, was_created = HistoricalPurchase.objects.get_or_create(
                    client=data["client"],
                    purchase_date=data["purchase_date"],
                    description=data["description"],
                    amount=data["amount"],
                    currency=data["currency"],
                    defaults={"note": data["note"], "created_by": creator},
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Готово: {created} новых покупок, {skipped} уже существовало (пропущено)."
            )
        )
