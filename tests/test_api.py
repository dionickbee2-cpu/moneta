import hmac
import json
import time
import hashlib
from urllib.parse import urlencode

import pytest

import ai
import api
import storage

TOKEN = "123:TEST"
UID = 777


def sign(user_id=UID, auth_date=None, token=TOKEN):
    fields = {"auth_date": str(int(auth_date or time.time())), "query_id": "AAA",
              "user": json.dumps({"id": user_id, "first_name": "Тест"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def client(sheet, monkeypatch):
    monkeypatch.setattr(api, "TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setattr(api, "DEV_USER_ID", "")
    ai._usage.clear()
    c = api.app.test_client()
    c.environ_base["HTTP_X_TELEGRAM_INIT_DATA"] = sign()
    return c


def test_verify_init_data():
    assert api.verify_init_data(sign(), TOKEN)["id"] == UID
    assert api.verify_init_data(sign(token="other"), TOKEN) is None
    assert api.verify_init_data(sign(auth_date=time.time() - 3 * 86400), TOKEN) is None
    tampered = sign().replace("777", "778")
    assert api.verify_init_data(tampered, TOKEN) is None


def test_requires_auth(client):
    r = client.get("/api/transactions", headers={"X-Telegram-Init-Data": ""})
    assert r.status_code == 401
    # the old ?user_id= backdoor is gone
    r = client.get("/api/transactions?user_id=777", headers={"X-Telegram-Init-Data": ""})
    assert r.status_code == 401


def test_crud(client):
    r = client.post("/api/transactions", json={"type": "expense", "amount": 12, "category": "☕ Cafe",
                                               "description": "<img src=x onerror=alert(1)>"})
    assert r.status_code == 200
    tx_id = r.json["id"]
    txs = client.get("/api/transactions").json["transactions"]
    assert txs[0]["id"] == tx_id and txs[0]["description"].startswith("<img")

    assert client.patch(f"/api/transactions/{tx_id}", json={"amount": 15, "category": "🍽 Dining"}).status_code == 200
    assert client.get("/api/transactions").json["transactions"][0]["amount"] == 15
    assert client.delete(f"/api/transactions/{tx_id}").status_code == 200
    assert client.delete(f"/api/transactions/{tx_id}").status_code == 404
    assert client.get("/api/transactions").json["transactions"] == []


@pytest.mark.parametrize("body", [
    {"type": "expense", "amount": "nan"},
    {"type": "expense", "amount": -5},
    {"type": "expense", "amount": 1e9},
    {"type": "account", "amount": 5},
    {"type": "setting", "amount": 5},
])
def test_validation(client, body):
    assert client.post("/api/transactions", json=body).status_code == 400


def test_empty_body(client):
    assert client.post("/api/transactions", data="x", content_type="text/plain").status_code == 400


def test_import_flow(client, monkeypatch):
    storage.add_transaction(UID, storage.parse_date("01.09.2026"), 12.0, "☕ Cafe", "coffee", "expense")
    fake = [{"date": "01.09.2026", "amount": 12.0, "type": "expense", "description": "Cafe",
             "category": "☕ Cafe", "account": "Revolut"},
            {"date": "02.09.2026", "amount": 30.5, "type": "expense", "description": "REWE",
             "category": "🛒 Groceries", "account": "Revolut"},
            {"date": "02.09.2026", "amount": 30.5, "type": "expense", "description": "REWE",
             "category": "🛒 Groceries", "account": "Revolut"}]
    monkeypatch.setattr(ai, "parse_screenshots", lambda images, account: [dict(f) for f in fake])
    import io
    r = client.post("/api/import/parse", data={"account": "Revolut",
                                                "images": [(io.BytesIO(b"\xff\xd8\xff..."), "a.jpg")]},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    assert [t["duplicate"] for t in r.json["transactions"]] == [True, False, True]

    r = client.post("/api/import/commit", json={"transactions": [r.json["transactions"][1]]})
    assert r.json["saved"] == 1
    txs = client.get("/api/transactions").json["transactions"]
    assert {(t["description"], t["account"], t["source"]) for t in txs} >= {("REWE", "Revolut", "import")}


def test_import_quota(client, monkeypatch):
    monkeypatch.setattr(ai, "DAILY_IMPORTS", 1)
    monkeypatch.setattr(ai, "parse_screenshots", lambda images, account: [])
    import io
    data = lambda: {"images": [(io.BytesIO(b"x"), "a.jpg"), (io.BytesIO(b"y"), "b.jpg")]}
    r = client.post("/api/import/parse", data=data(), content_type="multipart/form-data")
    assert r.status_code == 502 and "лимит" in r.json["error"]


def test_chat(client, monkeypatch):
    seen = {}

    def fake_chat(rows, messages):
        seen["messages"] = messages
        return "Всё хорошо"
    monkeypatch.setattr(ai, "chat", fake_chat)
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Как дела?"}]})
    assert r.json == {"reply": "Всё хорошо"}


def test_accounts(client):
    accs = [{"id": "a1", "name": "Revolut", "type": "card", "balance": 100.5, "primary": True}]
    assert client.post("/api/accounts", json={"accounts": accs}).status_code == 200
    assert client.post("/api/accounts", json={"accounts": accs}).status_code == 200
    assert client.get("/api/accounts").json["accounts"] == accs
