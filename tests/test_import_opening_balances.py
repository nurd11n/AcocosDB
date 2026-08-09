"""Phase 4 — the pre-system opening-balance xlsx import: Russian headers,
phone-first-then-name client matching, validate-before-write, idempotent by
(client, currency, as_of_date), one transaction, never invents a client for
an unmatched row.
"""

from decimal import Decimal

import openpyxl
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.clients.models import Client, ClientOpeningBalance

pytestmark = pytest.mark.django_db

HEADERS = ["клиент или телефон", "сумма", "валюта", "дата", "заметка"]


def _write_xlsx(path, rows, headers=HEADERS):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return str(path)


def test_import_matches_by_phone_and_creates_opening_balance(tmp_path):
    cust = Client.objects.create(first_name="Роза", phone="+996700111222")
    path = _write_xlsx(
        tmp_path / "ob.xlsx",
        [["+996700111222", 99000, "KGS", "01.01.2026", "старый долг"]],
    )
    call_command("import_opening_balances", path, yes=True)

    ob = ClientOpeningBalance.objects.get(client=cust)
    assert ob.amount == Decimal("99000")
    assert ob.currency == "KGS"
    assert ob.note == "старый долг"


def test_import_matches_by_name_when_cell_is_not_phone_shaped(tmp_path):
    cust = Client.objects.create(first_name="Айгуль", phone="+996700333444")
    path = _write_xlsx(
        tmp_path / "ob.xlsx",
        [["Айгуль", 5000, "USD", "01.01.2026", ""]],
    )
    call_command("import_opening_balances", path, yes=True)

    ob = ClientOpeningBalance.objects.get(client=cust)
    assert ob.amount == Decimal("5000")
    assert ob.currency == "USD"


def test_phone_matching_works_with_and_without_996(tmp_path):
    cust = Client.objects.create(first_name="Нурлан", phone="+996700555666")
    path = _write_xlsx(
        tmp_path / "ob.xlsx",
        [["0700555666", 3000, "KGS", "01.01.2026", ""]],  # local format, no +996
    )
    call_command("import_opening_balances", path, yes=True)

    assert ClientOpeningBalance.objects.filter(client=cust, amount=Decimal("3000")).exists()


def test_rerunning_the_same_file_changes_nothing(tmp_path):
    cust = Client.objects.create(first_name="Бекзат", phone="+996700777888")
    path = _write_xlsx(
        tmp_path / "ob.xlsx",
        [["+996700777888", 15000, "KGS", "01.01.2026", "долг"]],
    )
    call_command("import_opening_balances", path, yes=True)
    call_command("import_opening_balances", path, yes=True)  # re-run the SAME file

    assert ClientOpeningBalance.objects.filter(client=cust).count() == 1
    assert ClientOpeningBalance.objects.get(client=cust).amount == Decimal("15000")


def test_reimport_with_corrected_amount_updates_in_place(tmp_path):
    cust = Client.objects.create(first_name="Гуля", phone="+996700999000")
    path1 = _write_xlsx(
        tmp_path / "v1.xlsx",
        [["+996700999000", 10000, "KGS", "01.01.2026", ""]],
    )
    call_command("import_opening_balances", path1, yes=True)

    path2 = _write_xlsx(
        tmp_path / "v2.xlsx",
        [["+996700999000", 12500, "KGS", "01.01.2026", "исправлено"]],
    )
    call_command("import_opening_balances", path2, yes=True)

    assert ClientOpeningBalance.objects.filter(client=cust).count() == 1
    ob = ClientOpeningBalance.objects.get(client=cust)
    assert ob.amount == Decimal("12500")
    assert ob.note == "исправлено"


def test_a_bad_row_blocks_the_whole_import(tmp_path):
    good = Client.objects.create(first_name="Хороший", phone="+996700111000")
    path = _write_xlsx(
        tmp_path / "bad.xlsx",
        [
            ["+996700111000", 5000, "KGS", "01.01.2026", ""],
            [
                "+996700000000",
                "не число",
                "KGS",
                "01.01.2026",
                "",
            ],  # bad row: unmatched + bad amount
        ],
    )
    with pytest.raises(CommandError):
        call_command("import_opening_balances", path, yes=True)

    # ONE transaction — even the valid first row must not be written.
    assert not ClientOpeningBalance.objects.filter(client=good).exists()


def test_unmatched_row_is_reported_not_invented(tmp_path, capsys):
    before = Client.objects.count()
    path = _write_xlsx(
        tmp_path / "unmatched.xlsx",
        [["+996700123123", 1000, "KGS", "01.01.2026", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_opening_balances", path, yes=True)

    assert Client.objects.count() == before  # no client silently created
    assert not ClientOpeningBalance.objects.exists()
    assert "не найден" in capsys.readouterr().err


def test_ambiguous_name_is_reported_not_guessed(tmp_path, capsys):
    Client.objects.create(first_name="Дублёр", phone="+996700100200")
    Client.objects.create(first_name="Дублёр", phone="+996700100201")
    path = _write_xlsx(
        tmp_path / "ambiguous.xlsx",
        [["Дублёр", 1000, "KGS", "01.01.2026", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_opening_balances", path, yes=True)

    assert not ClientOpeningBalance.objects.exists()
    assert "нескольким клиентам" in capsys.readouterr().err


def test_unknown_currency_rejected(tmp_path):
    Client.objects.create(first_name="Валютный", phone="+996700200300")
    path = _write_xlsx(
        tmp_path / "badcur.xlsx",
        [["+996700200300", 1000, "EUR", "01.01.2026", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_opening_balances", path, yes=True)
    assert not ClientOpeningBalance.objects.exists()


def test_zero_amount_rejected(tmp_path):
    Client.objects.create(first_name="Нулевой", phone="+996700300400")
    path = _write_xlsx(
        tmp_path / "zero.xlsx",
        [["+996700300400", 0, "KGS", "01.01.2026", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_opening_balances", path, yes=True)


def test_unparseable_date_rejected(tmp_path):
    Client.objects.create(first_name="Датный", phone="+996700400500")
    path = _write_xlsx(
        tmp_path / "baddate.xlsx",
        [["+996700400500", 1000, "KGS", "не дата", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_opening_balances", path, yes=True)


def test_missing_required_header_rejected(tmp_path):
    path = _write_xlsx(
        tmp_path / "noheaders.xlsx",
        [["+996700500600", 1000, "KGS", "01.01.2026"]],
        headers=["клиент или телефон", "сумма", "валюта", "дата"],
    )
    with pytest.raises(CommandError, match="заметка"):
        call_command("import_opening_balances", path, yes=True)


def test_dry_run_writes_nothing(tmp_path):
    Client.objects.create(first_name="Сухой", phone="+996700600700")
    path = _write_xlsx(
        tmp_path / "dry.xlsx",
        [["+996700600700", 1000, "KGS", "01.01.2026", ""]],
    )
    call_command("import_opening_balances", path, dry_run=True)
    assert not ClientOpeningBalance.objects.exists()


def test_blank_trailing_rows_are_skipped_not_errors(tmp_path):
    cust = Client.objects.create(first_name="Хвостик", phone="+996700700800")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    ws.append(["+996700700800", 1000, "KGS", "01.01.2026", ""])
    ws.append([None] * 5)  # a genuinely blank row, e.g. left over in the sheet
    path = tmp_path / "blank.xlsx"
    wb.save(path)
    call_command("import_opening_balances", str(path), yes=True)
    assert ClientOpeningBalance.objects.filter(client=cust).exists()


def test_without_yes_flag_prompts_and_declining_writes_nothing(tmp_path, monkeypatch):
    Client.objects.create(first_name="Спросить", phone="+996700800900")
    path = _write_xlsx(
        tmp_path / "prompt.xlsx",
        [["+996700800900", 1000, "KGS", "01.01.2026", ""]],
    )
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    call_command("import_opening_balances", path)
    assert not ClientOpeningBalance.objects.exists()


def test_created_by_recorded_when_user_passed(tmp_path, django_user_model):
    Client.objects.create(first_name="Автор", phone="+996700900100")
    user = django_user_model.objects.create_user("importer", password="x" * 12)
    path = _write_xlsx(
        tmp_path / "ob.xlsx",
        [["+996700900100", 1000, "KGS", "01.01.2026", ""]],
    )
    call_command("import_opening_balances", path, yes=True, user="importer")
    ob = ClientOpeningBalance.objects.get()
    assert ob.created_by == user
