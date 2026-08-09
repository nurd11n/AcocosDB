"""Shared parsing helpers for the pre-system bulk-import commands
(import_opening_balances, import_purchase_history) — one implementation of
"what does a cell mean" so the two commands can't drift apart on date
formats or phone-vs-name detection. The leading underscore keeps Django's
management-command autodiscovery from registering this as a command
(pkgutil.iter_modules skips names starting with "_")."""

import re
from datetime import date, datetime

_PHONE_SHAPE = re.compile(r"^[\d\s\-()+.]+$")
_DATE_FORMATS = ["%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"]


def clean(value) -> str:
    return str(value).strip() if value is not None else ""


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean(value)
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def looks_like_phone(text: str) -> bool:
    digits = "".join(ch for ch in text if ch.isdigit())
    return len(digits) >= 6 and bool(_PHONE_SHAPE.match(text))
