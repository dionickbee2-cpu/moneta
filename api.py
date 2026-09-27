"""
Finance Bot API — Flask server for the Telegram Mini App.
Every /api/* request must carry Telegram WebApp initData in X-Telegram-Init-Data.
"""

import os
import json
import hmac
import math
import time
import hashlib
import logging
import threading
from functools import wraps
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify, g
from flask_cors import CORS

import ai
import sessions
import storage

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
DEV_USER_ID = os.getenv("DEV_USER_ID", "")  # local testing only: skips initData check
# Auth is a signed header, not a cookie, so CORS is not a security boundary here
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGIN", "*").split(",") if o.strip()]
INIT_DATA_MAX_AGE = 24 * 3600
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://dionickbee2-cpu.github.io/moneta/")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
CORS(app, origins=ALLOWED_ORIGINS, allow_headers=["Content-Type", "X-Telegram-Init-Data", "Authorization"],
     methods=["GET", "POST", "PATCH", "DELETE"])


# ── AUTH ──────────────────────────────────────────────────────────────────────
def verify_init_data(init_data: str, token: str, now: float = None):
    """User dict from Telegram WebApp initData, or None if the signature is invalid or stale."""
    if not init_data or not token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    if (now or time.time()) - auth_date > INIT_DATA_MAX_AGE:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def current_user_id():
    """Telegram initData inside Telegram, or a personal link token (Bearer) in a browser."""
    user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""), TELEGRAM_TOKEN)
    if user:
        return user["id"]
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return sessions.verify_token(auth[7:])
    if DEV_USER_ID:
        return int(DEV_USER_ID)
    return None


def require_user(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            g.uid = current_user_id()
            if g.uid is None:
                return jsonify({"error": "Откройте приложение через Telegram-бота или по личной ссылке из команды /app."}), 401
            return fn(*args, **kwargs)
        except storage.UserLimitError:
            return jsonify({"error": "Достигнут лимит пользователей бота."}), 503
        except storage.StorageError:
            return jsonify({"error": "Хранилище временно недоступно, попробуйте позже."}), 503
        except ai.AIError as e:
            return jsonify({"error": str(e)}), 502
    return wrapper


class BadInput(ValueError):
    pass


@app.errorhandler(BadInput)
def bad_input(e):
    return jsonify({"error": str(e)}), 400


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Файлы слишком большие (максимум 20 МБ за раз)."}), 413


# ── VALIDATION ────────────────────────────────────────────────────────────────
def json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise BadInput("Ожидался JSON-объект.")
    return data


def v_amount(val) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        raise BadInput("Некорректная сумма.")
    if not math.isfinite(f) or f <= 0 or f > 1_000_000:
        raise BadInput("Сумма должна быть больше 0 и не больше 1 000 000.")
    return round(f, 2)


def v_type(val) -> str:
    if val not in storage.TX_TYPES:
        raise BadInput("Тип должен быть expense, income или transfer.")
    return val


def v_date(val):
    if not val:
        return storage.now_local().date()
    d = storage.parse_date(val)
    if not d:
        raise BadInput("Некорректная дата.")
    return d


def v_text(val, limit=120) -> str:
    return str(val or "").strip()[:limit]


def tx_from_body(data: dict, source: str) -> dict:
    tx_type = v_type(data.get("type"))
    category = v_text(data.get("category"), 60) or ("💡 Other income" if tx_type == "income" else "📦 Other")
    return storage.make_record(
        g.uid, v_date(data.get("date")), v_amount(data.get("amount")), category,
        v_text(data.get("description")) or storage.plain_cat(category), tx_type,
        account=v_text(data.get("account"), 40), source=source,
    )


def tx_json(r: dict) -> dict:
    return {"id": r["TxID"], "date": r["Date"], "amount": r["Amount"], "category": r["Category"],
            "description": r["Description"], "type": r["Type"], "month": r["Month"],
            "account": r["Account"], "source": r["Source"]}


# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "sheets_connected": storage.is_available()})


@app.route("/api/transactions", methods=["GET"])
@require_user
def get_transactions():
    rows = storage.read_rows(g.uid)
    return jsonify({
        "transactions": [tx_json(r) for r in rows if r["Type"] in storage.TX_TYPES],
        "accounts": accounts_from_rows(rows),
    })


@app.route("/api/transactions", methods=["POST"])
@require_user
def add_transaction():
    rec = tx_from_body(json_body(), "app")
    storage.append_records(g.uid, [rec])
    return jsonify({"success": True, "id": rec["TxID"]})


@app.route("/api/transactions/<tx_id>", methods=["PATCH"])
@require_user
def update_transaction(tx_id):
    data = json_body()
    fields = {}
    if "amount" in data:
        fields["Amount"] = v_amount(data["amount"])
    if "type" in data:
        fields["Type"] = v_type(data["type"])
    if "date" in data:
        fields["Date"] = v_date(data["date"]).strftime("%d.%m.%Y")
    if "category" in data:
        fields["Category"] = v_text(data["category"], 60)
    if "description" in data:
        fields["Description"] = v_text(data["description"])
    if "account" in data:
        fields["Account"] = v_text(data["account"], 40)
    if not storage.update_tx(g.uid, tx_id, fields):
        return jsonify({"error": "Запись не найдена."}), 404
    return jsonify({"success": True})


@app.route("/api/transactions/<tx_id>", methods=["DELETE"])
@require_user
def delete_transaction(tx_id):
    if not storage.delete_tx(g.uid, tx_id):
        return jsonify({"error": "Запись не найдена."}), 404
    return jsonify({"success": True})


def mark_duplicates(candidates: list, existing: list) -> list:
    """Flag candidates that match an existing transaction or an earlier candidate (date + amount)."""
    seen = {(r["Date"], round(r["Amount"], 2)) for r in existing if r["Type"] in storage.TX_TYPES}
    for c in candidates:
        key = (c["date"], round(c["amount"], 2))
        c["duplicate"] = key in seen
        seen.add(key)
    return candidates


# Recognition takes up to a few minutes, longer than a phone keeps a request open,
# so it runs in a background job that the app polls.
IMPORT_JOB_TTL = 3600
_jobs = {}  # job id -> {"uid", "status": running|done|error, "result", "error", "created"}
_jobs_lock = threading.Lock()


def _run_import(job_id: str, uid: int, files: list, date_from):
    try:
        candidates = ai.parse_statement(files, date_from)
        update = {"status": "done", "result": mark_duplicates(candidates, storage.read_rows(uid))}
    except ai.AIError as e:
        ai.refund_quota(uid, "import", len(files))
        update = {"status": "error", "error": str(e)}
    except storage.StorageError:
        update = {"status": "error", "error": "Хранилище временно недоступно, попробуйте позже."}
    except Exception:
        logger.exception("Import job failed")
        ai.refund_quota(uid, "import", len(files))
        update = {"status": "error", "error": "Не удалось обработать файлы, попробуйте ещё раз."}
    with _jobs_lock:
        _jobs[job_id].update(update)


@app.route("/api/import/parse", methods=["POST"])
@require_user
def import_parse():
    uploads = request.files.getlist("files") or request.files.getlist("images")
    if not uploads:
        raise BadInput("Прикрепите хотя бы один скрин, PDF или CSV.")
    files = [(f.filename or "file", f.read()) for f in uploads]
    try:
        ai.file_blocks(files)  # validate types and sizes before spending quota
    except ai.AIError as e:
        raise BadInput(str(e))
    date_from = v_date(request.form["date_from"]) if request.form.get("date_from") else None
    now = time.time()
    with _jobs_lock:
        for jid in [j for j, job in _jobs.items() if now - job["created"] > IMPORT_JOB_TTL]:
            del _jobs[jid]
        running = next((j for j, job in _jobs.items() if job["uid"] == g.uid and job["status"] == "running"), None)
    if running:
        return jsonify({"job_id": running, "already_running": True}), 202
    ai.take_quota(g.uid, "import", len(files))
    job_id = storage.new_id() + storage.new_id()
    with _jobs_lock:
        _jobs[job_id] = {"uid": g.uid, "status": "running", "created": now}
    threading.Thread(target=_run_import, args=(job_id, g.uid, files, date_from), daemon=True).start()
    return jsonify({"job_id": job_id}), 202


@app.route("/api/import/jobs/<job_id>", methods=["GET"])
@require_user
def import_job(job_id):
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if not job or job["uid"] != g.uid:
        return jsonify({"status": "error", "error": "Задача не найдена — загрузите файлы ещё раз."}), 404
    body = {"status": job["status"], "elapsed": int(time.time() - job["created"])}
    if job["status"] == "done":
        body["transactions"] = job["result"]
    elif job["status"] == "error":
        body["error"] = job["error"]
    return jsonify(body)


@app.route("/api/import/commit", methods=["POST"])
@require_user
def import_commit():
    items = json_body().get("transactions")
    if not isinstance(items, list) or not items:
        raise BadInput("Нет записей для сохранения.")
    if len(items) > 300:
        raise BadInput("Слишком много записей за раз.")
    records = [tx_from_body(it, "import") for it in items if isinstance(it, dict)]
    ids = storage.append_records(g.uid, records)
    return jsonify({"success": True, "saved": len(ids)})


@app.route("/api/chat", methods=["POST"])
@require_user
def chat():
    messages = json_body().get("messages")
    if not isinstance(messages, list):
        raise BadInput("Ожидался список сообщений.")
    ai.take_quota(g.uid, "chat")
    reply = ai.chat(storage.read_rows(g.uid), messages)
    return jsonify({"reply": reply})


@app.route("/api/session/link", methods=["POST"])
@require_user
def session_link():
    """Personal link that opens the app in a browser, so it can be installed on the home screen."""
    return jsonify({"url": f"{WEBAPP_URL}#t={sessions.make_token(g.uid)}"})


# ── ACCOUNTS ──────────────────────────────────────────────────────────────────
# Stored as Type=account rows: Date=id, Amount=balance, Category=type, Description=name, Month='primary'
def accounts_from_rows(rows: list) -> list:
    return [{
        "id": r["Date"],
        "balance": storage.safe_float(r["Amount"]),
        "type": r["Category"] or "bank",
        "name": r["Description"],
        "primary": r["Month"] == "primary",
    } for r in rows if r["Type"] == "account"]


@app.route("/api/accounts", methods=["GET"])
@require_user
def get_accounts():
    return jsonify({"accounts": accounts_from_rows(storage.read_rows(g.uid))})


@app.route("/api/accounts", methods=["POST"])
@require_user
def save_accounts():
    accounts = json_body().get("accounts")
    if not isinstance(accounts, list) or len(accounts) > 50:
        raise BadInput("Ожидался список счетов.")
    records = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        balance = storage.safe_float(acc.get("balance"))
        rec = storage.make_record(g.uid, storage.now_local(), round(balance, 2),
                                  v_text(acc.get("type"), 20) or "bank",
                                  v_text(acc.get("name"), 60), "account", source="app")
        rec["Date"] = v_text(acc.get("id"), 40) or rec["TxID"]
        rec["Month"] = "primary" if acc.get("primary") else ""
        records.append(rec)
    storage.replace_meta(g.uid, "account", records)
    return jsonify({"success": True})


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
