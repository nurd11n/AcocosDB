"""Pre-system purchase-history xlsx import: Russian headers, phone-first-
then-name client matching, validate-before-write, content-based idempotency
(NOT upsert — a purchase is an event, not a snapshot value), one
transaction, never invents a client, never touches debt/stock/revenue.
"""

from datetime import date
from decimal import Decimal

import openpyxl
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.clients.models import Client, HistoricalPurchase

pytestmark = pytest.mark.django_db

HEADERS = ["клиент или телефон", "дата", "описание", "сумма", "валюта", "заметка"]


def _write_xlsx(path, rows, headers=HEADERS):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return str(path)


def test_import_matches_by_phone_and_creates_historical_purchase(tmp_path):
    cust = Client.objects.create(first_name="Роза", phone="+996700111222")
    path = _write_xlsx(
        tmp_path / "ph.xlsx",
        [["+996700111222", "15.03.2025", "3 вечерних платья L", 45000, "KGS", "старая покупка"]],
    )
    call_command("import_purchase_history", path, yes=True)

    hp = HistoricalPurchase.objects.get(client=cust)
    assert hp.purchase_date == date(2025, 3, 15)
    assert hp.description == "3 вечерних платья L"
    assert hp.amount == Decimal("45000")
    assert hp.currency == "KGS"
    assert hp.note == "старая покупка"


def test_import_matches_by_name_when_cell_is_not_phone_shaped(tmp_path):
    cust = Client.objects.create(first_name="Айгуль", phone="+996700333444")
    path = _write_xlsx(
        tmp_path / "ph.xlsx",
        [["Айгуль", "01.01.2025", "Костюм на заказ", 5000, "USD", ""]],
    )
    call_command("import_purchase_history", path, yes=True)
    assert HistoricalPurchase.objects.filter(client=cust, currency="USD").exists()


def test_phone_matching_works_with_and_without_996(tmp_path):
    cust = Client.objects.create(first_name="Нурлан", phone="+996700555666")
    path = _write_xlsx(
        tmp_path / "ph.xlsx",
        [["0700555666", "01.01.2025", "Платье", 3000, "KGS", ""]],  # local format
    )
    call_command("import_purchase_history", path, yes=True)
    assert HistoricalPurchase.objects.filter(client=cust, amount=Decimal("3000")).exists()


def test_rerunning_the_same_file_is_a_no_op(tmp_path):
    cust = Client.objects.create(first_name="Бекзат", phone="+996700777888")
    path = _write_xlsx(
        tmp_path / "ph.xlsx",
        [["+996700777888", "01.01.2025", "Платье", 15000, "KGS", "долг"]],
    )
    call_command("import_purchase_history", path, yes=True)
    call_command("import_purchase_history", path, yes=True)  # re-run the SAME file

    assert HistoricalPurchase.objects.filter(client=cust).count() == 1


def test_two_different_purchases_same_client_same_date_both_kept(tmp_path):
    """Unlike opening balances, a purchase is an EVENT — the same client can
    have two genuinely different purchases on the same day, and both must
    be kept (never collapsed into one by an (client, date) upsert key)."""
    cust = Client.objects.create(first_name="Дважды", phone="+996700888999")
    path = _write_xlsx(
        tmp_path / "ph.xlsx",
        [
            ["+996700888999", "01.01.2025", "Платье А", 3000, "KGS", ""],
            ["+996700888999", "01.01.2025", "Платье Б", 4000, "KGS", ""],
        ],
    )
    call_command("import_purchase_history", path, yes=True)
    assert HistoricalPurchase.objects.filter(client=cust).count() == 2


def test_a_bad_row_blocks_the_whole_import(tmp_path):
    good = Client.objects.create(first_name="Хороший", phone="+996700111000")
    path = _write_xlsx(
        tmp_path / "bad.xlsx",
        [
            ["+996700111000", "01.01.2025", "Платье", 5000, "KGS", ""],
            [
                "+996700000000",
                "01.01.2025",
                "Платье",
                "не число",
                "KGS",
                "",
            ],  # unmatched + bad amount
        ],
    )
    with pytest.raises(CommandError):
        call_command("import_purchase_history", path, yes=True)

    assert not HistoricalPurchase.objects.filter(client=good).exists()


def test_unmatched_row_is_reported_not_invented(tmp_path, capsys):
    before = Client.objects.count()
    path = _write_xlsx(
        tmp_path / "unmatched.xlsx",
        [["+996700123123", "01.01.2025", "Платье", 1000, "KGS", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_purchase_history", path, yes=True)

    assert Client.objects.count() == before
    assert not HistoricalPurchase.objects.exists()
    assert "не найден" in capsys.readouterr().err


def test_ambiguous_name_is_reported_not_guessed(tmp_path, capsys):
    Client.objects.create(first_name="Дублёр", phone="+996700100200")
    Client.objects.create(first_name="Дублёр", phone="+996700100201")
    path = _write_xlsx(
        tmp_path / "ambiguous.xlsx",
        [["Дублёр", "01.01.2025", "Платье", 1000, "KGS", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_purchase_history", path, yes=True)
    assert not HistoricalPurchase.objects.exists()
    assert "нескольким клиентам" in capsys.readouterr().err


def test_empty_description_rejected(tmp_path):
    Client.objects.create(first_name="Пустой", phone="+996700200300")
    path = _write_xlsx(
        tmp_path / "nodesc.xlsx",
        [["+996700200300", "01.01.2025", "", 1000, "KGS", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_purchase_history", path, yes=True)


def test_zero_or_negative_amount_rejected(tmp_path):
    Client.objects.create(first_name="Ноль", phone="+996700300400")
    path = _write_xlsx(
        tmp_path / "zero.xlsx",
        [["+996700300400", "01.01.2025", "Платье", 0, "KGS", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_purchase_history", path, yes=True)


def test_unknown_currency_rejected(tmp_path):
    Client.objects.create(first_name="Валютный", phone="+996700400500")
    path = _write_xlsx(
        tmp_path / "badcur.xlsx",
        [["+996700400500", "01.01.2025", "Платье", 1000, "EUR", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_purchase_history", path, yes=True)


def test_unparseable_date_rejected(tmp_path):
    Client.objects.create(first_name="Датный", phone="+996700500600")
    path = _write_xlsx(
        tmp_path / "baddate.xlsx",
        [["+996700500600", "не дата", "Платье", 1000, "KGS", ""]],
    )
    with pytest.raises(CommandError):
        call_command("import_purchase_history", path, yes=True)


def test_missing_required_header_rejected(tmp_path):
    path = _write_xlsx(
        tmp_path / "noheaders.xlsx",
        [["+996700600700", "01.01.2025", "Платье", 1000, "KGS"]],
        headers=["клиент или телефон", "дата", "описание", "сумма", "валюта"],
    )
    with pytest.raises(CommandError, match="заметка"):
        call_command("import_purchase_history", path, yes=True)


def test_dry_run_writes_nothing(tmp_path):
    Client.objects.create(first_name="Сухой", phone="+996700700800")
    path = _write_xlsx(
        tmp_path / "dry.xlsx",
        [["+996700700800", "01.01.2025", "Платье", 1000, "KGS", ""]],
    )
    call_command("import_purchase_history", path, dry_run=True)
    assert not HistoricalPurchase.objects.exists()


def test_blank_trailing_rows_are_skipped_not_errors(tmp_path):
    cust = Client.objects.create(first_name="Хвостик", phone="+996700800900")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    ws.append(["+996700800900", "01.01.2025", "Платье", 1000, "KGS", ""])
    ws.append([None] * 6)  # a genuinely blank row
    path = tmp_path / "blank.xlsx"
    wb.save(path)
    call_command("import_purchase_history", str(path), yes=True)
    assert HistoricalPurchase.objects.filter(client=cust).exists()


def test_without_yes_flag_prompts_and_declining_writes_nothing(tmp_path, monkeypatch):
    Client.objects.create(first_name="Спросить", phone="+996700900100")
    path = _write_xlsx(
        tmp_path / "prompt.xlsx",
        [["+996700900100", "01.01.2025", "Платье", 1000, "KGS", ""]],
    )
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    call_command("import_purchase_history", path)
    assert not HistoricalPurchase.objects.exists()


def test_created_by_recorded_when_user_passed(tmp_path, django_user_model):
    Client.objects.create(first_name="Автор", phone="+996700900200")
    user = django_user_model.objects.create_user("ph_importer", password="x" * 12)
    path = _write_xlsx(
        tmp_path / "ph.xlsx",
        [["+996700900200", "01.01.2025", "Платье", 1000, "KGS", ""]],
    )
    call_command("import_purchase_history", path, yes=True, user="ph_importer")
    hp = HistoricalPurchase.objects.get()
    assert hp.created_by == user


def test_never_affects_client_debt_or_debts_by_currency(tmp_path):
    from apps.clients.services import client_debt, client_debts_by_currency

    cust = Client.objects.create(first_name="БезВлияния", phone="+996700900300")
    path = _write_xlsx(
        tmp_path / "ph.xlsx",
        [["+996700900300", "01.01.2025", "Дорогое платье", 500000, "KGS", ""]],
    )
    call_command("import_purchase_history", path, yes=True)

    assert client_debt(cust) == {}
    assert client_debts_by_currency(client=cust).get(cust.pk, {}) == {}
