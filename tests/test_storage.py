from datetime import date

import storage
from conftest import FakeWorksheet

UID = 42


def test_new_sheet_roundtrip(sheet):
    tx_id = storage.add_transaction(UID, date(2026, 9, 1), 4.5, "☕ Cafe", "coffee", "expense")
    rows = storage.read_transactions(UID)
    assert len(rows) == 1
    r = rows[0]
    assert (r["TxID"], r["Date"], r["Amount"], r["Month"], r["Type"]) == (tx_id, "01.09.2026", 4.5, "2026-09", "expense")
    assert sheet.sheets["user_42"].rows[0] == storage.HEADERS


def test_legacy_russian_sheet(sheet):
    ws = FakeWorksheet("user_42", [
        ["Дата", "Сумма (€)", "Категория", "Описание", "Тип", "Месяц"],
        ["31.05.2026", "45,0", "🛒 Продукты", "Продукты", "расход", "2026-05"],
        ["01.06.2026", 3000, "💼 Salary", "", "доход", ""],
    ], cols=6)
    sheet.sheets["user_42"] = ws
    rows = storage.read_transactions(UID)
    assert [r["Type"] for r in rows] == ["expense", "income"]
    assert rows[0]["Amount"] == 45.0
    assert rows[1]["Month"] == "2026-06"
    # missing columns appended and every row got a persistent id
    assert ws.rows[0][6:] == ["UserID", "TxID", "Account", "Source"]
    assert ws.col_count >= 10
    ids = [r["TxID"] for r in rows]
    assert all(ids) and [r["TxID"] for r in storage.read_transactions(UID)] == ids
    exp, inc, by_cat = storage.month_stats(rows, "2026-05")
    assert (exp, inc) == (45.0, 0)


def test_delete_and_update_by_id(sheet):
    a = storage.add_transaction(UID, date(2026, 9, 1), 12, "☕ Cafe", "first", "expense")
    b = storage.add_transaction(UID, date(2026, 9, 1), 12, "☕ Cafe", "second", "expense")
    assert storage.delete_tx(UID, b)
    assert [r["Description"] for r in storage.read_transactions(UID)] == ["first"]
    assert storage.update_tx(UID, a, {"Amount": 13.5, "Date": "02.10.2026", "Category": "🍽 Dining"})
    r = storage.read_transactions(UID)[0]
    assert (r["Amount"], r["Month"], r["Category"]) == (13.5, "2026-10", "🍽 Dining")
    assert not storage.delete_tx(UID, "nope")


def test_settings_replace(sheet):
    storage.set_setting(UID, "reminder_time", "19:30")
    storage.set_setting(UID, "reminder_time", "08:00")
    rows = storage.read_rows(UID)
    assert [r["Description"] for r in rows if r["Type"] == "setting"] == ["08:00"]


def test_user_limit(sheet, monkeypatch):
    monkeypatch.setattr(storage, "MAX_USERS", 1)
    storage.get_user_sheet(1)
    try:
        storage.get_user_sheet(2)
        assert False, "expected UserLimitError"
    except storage.UserLimitError:
        pass


def test_delete_last_sheet_clears(sheet, monkeypatch):
    storage.add_transaction(UID, date(2026, 9, 1), 1, "☕ Cafe", "x", "expense")

    def refuse(ws):
        import gspread
        raise gspread.exceptions.APIError.__new__(gspread.exceptions.APIError)
    monkeypatch.setattr(sheet, "del_worksheet", refuse)
    assert storage.delete_user_data(UID) == "cleared"
    assert storage.read_transactions(UID) == []
