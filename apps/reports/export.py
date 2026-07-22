"""Spreadsheet serialization shared by the storage and dashboard exports.

One place that turns {sheet_name: rows} into an .xlsx workbook or a single sheet
into UTF-8-BOM .csv (BOM so Excel renders Cyrillic correctly). Both the storage
report and the analytics dashboard render from the same aggregates they show on
screen, then hand the rows here — so a download can never disagree with the page.
"""

import csv
import io

from openpyxl import Workbook


def to_xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    for i, (name, rows) in enumerate(sheets.items()):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = name[:31]  # Excel caps sheet names at 31 chars
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_csv_bytes(rows: list[list]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")
