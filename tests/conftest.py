import os
import sys

import pytest
import gspread
from gspread.utils import a1_to_rowcol

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402


class FakeWorksheet:
    """In-memory stand-in for gspread.Worksheet (only the methods storage.py uses)."""

    def __init__(self, title, rows=None, cols=10):
        self.title = title
        self.rows = [list(r) for r in (rows or [])]
        self.col_count = cols

    def get_all_values(self, value_render_option=None):
        width = max((len(r) for r in self.rows), default=0)
        return [list(r) + [""] * (width - len(r)) for r in self.rows]

    def append_row(self, row, **kw):
        self.rows.append(list(row))

    def append_rows(self, rows, value_input_option=None):
        self.rows.extend(list(r) for r in rows)

    def _set(self, a1, value):
        r, c = a1_to_rowcol(a1)
        while len(self.rows) < r:
            self.rows.append([])
        row = self.rows[r - 1]
        while len(row) < c:
            row.append("")
        row[c - 1] = value

    def update(self, values, range_name=None, raw=True):
        start = range_name.split(":")[0]
        r, c = a1_to_rowcol(start)
        for i, vals in enumerate(values):
            for j, v in enumerate(vals):
                self._set(gspread.utils.rowcol_to_a1(r + i, c + j), v)

    def batch_update(self, data):
        for item in data:
            self.update(item["values"], item["range"])

    def delete_rows(self, n):
        del self.rows[n - 1]

    def add_cols(self, n):
        self.col_count += n

    def clear(self):
        self.rows = []


class FakeSpreadsheet:
    def __init__(self):
        self.sheets = {}

    def worksheet(self, title):
        if title not in self.sheets:
            raise gspread.WorksheetNotFound(title)
        return self.sheets[title]

    def worksheets(self):
        return list(self.sheets.values())

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(title, cols=cols)
        self.sheets[title] = ws
        return ws

    def del_worksheet(self, ws):
        del self.sheets[ws.title]


@pytest.fixture
def sheet(monkeypatch):
    sp = FakeSpreadsheet()
    monkeypatch.setattr(storage, "_spreadsheet", sp)
    storage._sheets.clear()
    storage._layouts.clear()
    return sp
