"""
Google Sheets storage shared by the Telegram bot and the Flask API.

Each user has a worksheet "user_<telegram id>". Rows hold transactions and
meta records (budgets, settings, accounts), told apart by the Type column.
Columns are located by header name, so legacy sheets with Russian headers
keep working; missing columns are appended to the header row on first use.
"""

import os
import json
import math
import uuid
import logging
import threading
from collections import defaultdict
from datetime import datetime, date, timedelta

import pytz

logger = logging.getLogger(__name__)

TIMEZONE = pytz.timezone(os.getenv("BOT_TIMEZONE", "Europe/Berlin"))
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Finance Bot Data")
MAX_USERS = int(os.getenv("MAX_USERS", "300"))

HEADERS = ["Date", "Amount", "Category", "Description", "Type", "Month",
           "UserID", "TxID", "Account", "Source"]
TX_TYPES = ("expense", "income", "transfer")
META_TYPES = ("budget", "template", "setting", "account")

EXPENSE_CATS = [
    "🛒 Groceries", "☕ Cafe", "🏠 Rent", "🚌 Transport", "📱 Subscriptions", "🍽 Dining",
    "👕 Clothing", "💊 Health", "🎮 Entertainment", "🏋 Sports", "✈️ Travel",
    "📚 Education", "🔧 Home", "💰 Investments", "📦 Other",
]
INCOME_CATS = ["💼 Salary", "🖥 Freelance", "📈 Dividends", "🎁 Gift", "🏠 Rental income", "💡 Other income"]

CAT_RU = {
    "Groceries": "Продукты", "Cafe": "Кафе", "Rent": "Аренда", "Transport": "Транспорт",
    "Subscriptions": "Подписки", "Dining": "Рестораны", "Clothing": "Одежда",
    "Health": "Здоровье", "Entertainment": "Развлечения", "Sports": "Спорт",
    "Travel": "Путешествия", "Education": "Образование", "Home": "Дом",
    "Investments": "Инвестиции", "Other": "Другое", "Salary": "Зарплата",
    "Freelance": "Фриланс", "Dividends": "Дивиденды", "Gift": "Подарок",
    "Rental income": "Доход от аренды", "Other income": "Другой доход",
}


def plain_cat(cat: str) -> str:
    """'🛒 Groceries' -> 'Groceries'."""
    parts = str(cat).split(" ", 1)
    if len(parts) == 2 and not parts[0].isalnum():
        return parts[1]
    return str(cat)


# plain English name -> stored name with emoji
CAT_BY_NAME = {plain_cat(c): c for c in EXPENSE_CATS + INCOME_CATS}


def cat_label(cat: str) -> str:
    """Russian label for display: '🛒 Groceries' -> '🛒 Продукты'."""
    name = plain_cat(cat)
    ru = CAT_RU.get(name)
    if not ru:
        return str(cat)
    icon = str(cat)[: len(str(cat)) - len(name)].strip()
    return f"{icon} {ru}".strip()


class StorageError(Exception):
    """Google Sheets is unavailable or refused the operation."""


class UserLimitError(StorageError):
    """No room for another user's worksheet."""


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def safe_float(val, default=0.0) -> float:
    """Float from a sheet cell: handles numbers, '4,5', '1 234.5' and blanks."""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        f = float(val)
    else:
        try:
            f = float(str(val).replace(" ", "").replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            return default
    return f if math.isfinite(f) else default


def parse_date(val):
    """Date from a sheet cell ('25.05.2026', '25.05.26', '2026-05-25' or a serial number)."""
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return (date(1899, 12, 30) + timedelta(days=int(val))) if val > 0 else None
    s = str(val or "").strip()
    try:
        if "." in s:
            d, m, y = s.split(".")[:3]
            y = int(y)
            return date(y + 2000 if y < 100 else y, int(m), int(d))
        if "-" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        pass
    return None


_HEADER_ALIASES = {
    "дата": "Date", "date": "Date",
    "сумма (€)": "Amount", "сумма": "Amount", "amount": "Amount",
    "категория": "Category", "category": "Category",
    "описание": "Description", "description": "Description",
    "тип": "Type", "type": "Type",
    "месяц": "Month", "month": "Month",
    "userid": "UserID", "user_id": "UserID",
    "txid": "TxID", "account": "Account", "source": "Source",
}


def normalize_type(raw) -> str:
    t = str(raw or "").lower().strip()
    if t in ("расход", "expense", "-"):
        return "expense"
    if t in ("доход", "income", "+"):
        return "income"
    if t in ("перевод", "transfer"):
        return "transfer"
    if t in ("бюджет", "budget"):
        return "budget"
    if t in ("шаблон", "template"):
        return "template"
    return t


def new_id() -> str:
    return uuid.uuid4().hex[:8]


# ── CONNECTION ────────────────────────────────────────────────────────────────
_init_lock = threading.Lock()
_user_locks = defaultdict(threading.RLock)
_spreadsheet = None
_sheets = {}   # uid -> worksheet
_layouts = {}  # uid -> {canonical header: column index}


def _api_error():
    import gspread
    return gspread.exceptions.APIError


def get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is not None:
        return _spreadsheet
    with _init_lock:
        if _spreadsheet is not None:
            return _spreadsheet
        creds_json = os.getenv("GOOGLE_CREDS_JSON")
        if not creds_json:
            raise StorageError("GOOGLE_CREDS_JSON is not set")
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            creds = Credentials.from_service_account_info(
                json.loads(creds_json),
                scopes=["https://www.googleapis.com/auth/spreadsheets",
                        "https://www.googleapis.com/auth/drive"],
            )
            _spreadsheet = gspread.authorize(creds).open(SPREADSHEET_NAME)
        except Exception as e:
            logger.error(f"Google Sheets connect error: {e}")
            raise StorageError(str(e)) from e
        return _spreadsheet


def is_available() -> bool:
    try:
        get_spreadsheet()
        return True
    except StorageError:
        return False


def list_user_ids() -> list:
    try:
        titles = [ws.title for ws in get_spreadsheet().worksheets()]
    except _api_error() as e:
        raise StorageError(str(e)) from e
    return [int(t[5:]) for t in titles if t.startswith("user_") and t[5:].isdigit()]


def get_user_sheet(uid: int, create: bool = True):
    """The user's worksheet; created on first use unless create=False (then None)."""
    if uid in _sheets:
        return _sheets[uid]
    import gspread
    sp = get_spreadsheet()
    title = f"user_{uid}"
    try:
        ws = sp.worksheet(title)
    except gspread.WorksheetNotFound:
        if not create:
            return None
        try:
            if len(list_user_ids()) >= MAX_USERS:
                raise UserLimitError("user limit reached")
            ws = sp.add_worksheet(title=title, rows=1000, cols=len(HEADERS))
            ws.append_row(HEADERS)
        except gspread.exceptions.APIError as e:
            raise StorageError(str(e)) from e
    except gspread.exceptions.APIError as e:
        raise StorageError(str(e)) from e
    _sheets[uid] = ws
    return ws


def _forget(uid):
    _sheets.pop(uid, None)
    _layouts.pop(uid, None)


def _ensure_layout(uid, ws, header_row) -> dict:
    """Map canonical headers to column indexes, appending any missing headers."""
    layout = {}
    for i, h in enumerate(header_row):
        key = _HEADER_ALIASES.get(str(h).lower().strip())
        if key and key not in layout:
            layout[key] = i
    missing = [h for h in HEADERS if h not in layout]
    if missing:
        start = len(header_row)
        need = start + len(missing)
        if ws.col_count < need:
            ws.add_cols(need - ws.col_count)
        from gspread.utils import rowcol_to_a1
        ws.update(values=[missing], range_name=f"{rowcol_to_a1(1, start + 1)}:{rowcol_to_a1(1, need)}")
        for j, h in enumerate(missing):
            layout[h] = start + j
    _layouts[uid] = layout
    return layout


def _load(uid, create=True):
    """(worksheet, layout, rows) where rows are dicts with canonical keys and '_row'."""
    from gspread.utils import ValueRenderOption, rowcol_to_a1
    ws = get_user_sheet(uid, create=create)
    if ws is None:
        return None, {}, []
    try:
        values = ws.get_all_values(value_render_option=ValueRenderOption.unformatted)
        if not values:
            ws.append_row(HEADERS)
            values = [HEADERS]
        layout = _ensure_layout(uid, ws, values[0])
        rows, id_updates = [], []
        for n, raw in enumerate(values[1:], start=2):
            if not any(str(c).strip() for c in raw):
                continue
            r = {k: (raw[i] if i < len(raw) else "") for k, i in layout.items()}
            r["_row"] = n
            if not str(r.get("TxID", "")).strip():
                r["TxID"] = new_id()
                id_updates.append({"range": rowcol_to_a1(n, layout["TxID"] + 1), "values": [[r["TxID"]]]})
            rows.append(_clean(r))
        if id_updates:
            ws.batch_update(id_updates)
    except _api_error() as e:
        _forget(uid)
        raise StorageError(str(e)) from e
    return ws, layout, rows


def _clean(r: dict) -> dict:
    r["Type"] = normalize_type(r.get("Type"))
    d = parse_date(r.get("Date"))
    if r["Type"] != "account":
        r["Amount"] = safe_float(r.get("Amount"))
        if d:
            r["Date"] = d.strftime("%d.%m.%Y")
            if not str(r.get("Month", "")).strip():
                r["Month"] = d.strftime("%Y-%m")
    for k in ("Category", "Description", "Month", "Account", "Source", "TxID", "UserID"):
        r[k] = "" if r.get(k) is None else str(r.get(k))
    r["Date"] = str(r.get("Date", ""))
    return r


def _to_row(layout, rec: dict) -> list:
    row = [""] * (max(layout.values()) + 1)
    for k, v in rec.items():
        if k in layout:
            row[layout[k]] = v
    return row


# ── PUBLIC API ────────────────────────────────────────────────────────────────
def read_rows(uid: int) -> list:
    """All rows of the user (transactions and meta), without creating a sheet."""
    with _user_locks[uid]:
        return _load(uid, create=False)[2]


def read_transactions(uid: int) -> list:
    return [r for r in read_rows(uid) if r["Type"] in TX_TYPES]


def make_record(uid, when, amount, category, description, tx_type, account="", source="bot") -> dict:
    """Canonical record for a transaction or meta row. `when` is a date/datetime."""
    d = when.date() if isinstance(when, datetime) else when
    return {
        "Date": d.strftime("%d.%m.%Y"), "Amount": amount, "Category": category,
        "Description": description, "Type": tx_type, "Month": d.strftime("%Y-%m"),
        "UserID": str(uid), "TxID": new_id(), "Account": account, "Source": source,
    }


def append_records(uid: int, records: list) -> list:
    """Append records in one API call. Returns their TxIDs."""
    if not records:
        return []
    with _user_locks[uid]:
        ws, layout, _ = _load(uid)
        try:
            ws.append_rows([_to_row(layout, r) for r in records], value_input_option="RAW")
        except _api_error() as e:
            raise StorageError(str(e)) from e
    return [r["TxID"] for r in records]


def add_transaction(uid, when, amount, category, description, tx_type, account="", source="bot") -> str:
    return append_records(uid, [make_record(uid, when, amount, category, description, tx_type, account, source)])[0]


def _delete_rows(ws, row_numbers):
    for n in sorted(set(row_numbers), reverse=True):
        ws.delete_rows(n)


def delete_tx(uid: int, tx_id: str) -> bool:
    with _user_locks[uid]:
        ws, _, rows = _load(uid, create=False)
        row = next((r for r in rows if r["TxID"] == tx_id and r["Type"] in TX_TYPES), None)
        if not row:
            return False
        try:
            ws.delete_rows(row["_row"])
        except _api_error() as e:
            raise StorageError(str(e)) from e
        return True


EDITABLE = ("Date", "Amount", "Category", "Description", "Type", "Account")


def update_tx(uid: int, tx_id: str, fields: dict) -> bool:
    from gspread.utils import rowcol_to_a1
    with _user_locks[uid]:
        ws, layout, rows = _load(uid, create=False)
        row = next((r for r in rows if r["TxID"] == tx_id and r["Type"] in TX_TYPES), None)
        if not row:
            return False
        merged = {k: row.get(k, "") for k in HEADERS}
        merged.update({k: v for k, v in fields.items() if k in EDITABLE})
        d = parse_date(merged["Date"])
        if d:
            merged["Month"] = d.strftime("%Y-%m")
        values = _to_row(layout, merged)
        rng = f"{rowcol_to_a1(row['_row'], 1)}:{rowcol_to_a1(row['_row'], len(values))}"
        try:
            ws.update(values=[values], range_name=rng, raw=True)
        except _api_error() as e:
            raise StorageError(str(e)) from e
        return True


def replace_meta(uid: int, tx_type: str, records: list, match=None):
    """Replace meta rows of tx_type (optionally only those where match(row) is true).
    New rows are written first, so a failure never leaves the user without data."""
    with _user_locks[uid]:
        ws, layout, rows = _load(uid)
        old = [r["_row"] for r in rows if r["Type"] == tx_type and (match is None or match(r))]
        try:
            if records:
                ws.append_rows([_to_row(layout, r) for r in records], value_input_option="RAW")
            _delete_rows(ws, old)
        except _api_error() as e:
            raise StorageError(str(e)) from e


def get_setting(rows: list, key: str, default=None):
    row = next((r for r in reversed(rows) if r["Type"] == "setting" and r["Category"] == key), None)
    return row["Description"] if row else default


def set_setting(uid: int, key: str, value: str):
    rec = make_record(uid, now_local(), 0, key, value, "setting", source="bot")
    replace_meta(uid, "setting", [rec], match=lambda r: r["Category"] == key)


def delete_user_data(uid: int) -> str:
    """Remove all of the user's data. Returns 'deleted', 'cleared' or 'none'."""
    with _user_locks[uid]:
        ws = get_user_sheet(uid, create=False)
        _forget(uid)
        if ws is None:
            return "none"
        sp = get_spreadsheet()
        try:
            sp.del_worksheet(ws)
            return "deleted"
        except _api_error():
            # The last worksheet of a spreadsheet cannot be deleted: empty it instead
            try:
                ws.clear()
                ws.append_row(HEADERS)
                return "cleared"
            except _api_error() as e:
                raise StorageError(str(e)) from e


# ── STATS ─────────────────────────────────────────────────────────────────────
def month_stats(rows: list, month: str):
    """(expenses, income, {category: expense}) for 'YYYY-MM'."""
    exp = inc = 0.0
    by_cat = {}
    for r in rows:
        if r["Month"] != month:
            continue
        if r["Type"] == "expense":
            exp += r["Amount"]
            by_cat[r["Category"]] = by_cat.get(r["Category"], 0) + r["Amount"]
        elif r["Type"] == "income":
            inc += r["Amount"]
    return exp, inc, by_cat


def prev_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
