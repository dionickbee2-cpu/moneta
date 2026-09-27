import time

import pytest

import api
import sessions
import storage

UID = 555


@pytest.fixture(autouse=True)
def env(sheet, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:TEST")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    sessions._versions.clear()
    monkeypatch.setattr(api, "DEV_USER_ID", "")


def test_roundtrip_and_tamper():
    token = sessions.make_token(UID)
    assert sessions.verify_token(token) == UID
    body, sig = token.split(".")
    assert sessions.verify_token(body + "." + sig[:-2] + "AA") is None
    assert sessions.verify_token("garbage") is None
    assert sessions.verify_token("") is None


def test_expiry():
    token = sessions.make_token(UID, now=time.time() - sessions.TTL - 10)
    assert sessions.verify_token(token) is None


def test_secret_change_invalidates(monkeypatch):
    token = sessions.make_token(UID)
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:ROTATED")
    assert sessions.verify_token(token) is None


def test_revoke_all():
    old = sessions.make_token(UID)
    sessions.revoke_all(UID)
    assert sessions.verify_token(old) is None
    new = sessions.make_token(UID)
    assert sessions.verify_token(new) == UID
    # version persisted in the sheet, not only in memory
    sessions._versions.clear()
    assert sessions.verify_token(old) is None and sessions.verify_token(new) == UID
    assert storage.get_setting(storage.read_rows(UID), "session_version") == "1"


def test_api_bearer_and_link(monkeypatch):
    monkeypatch.setattr(api, "TELEGRAM_TOKEN", "123:TEST")
    c = api.app.test_client()
    token = sessions.make_token(UID)
    auth = {"Authorization": "Bearer " + token}
    assert c.get("/api/transactions", headers=auth).status_code == 200
    assert c.get("/api/transactions", headers={"Authorization": "Bearer x.y"}).status_code == 401
    r = c.post("/api/session/link", headers=auth)
    assert r.status_code == 200 and "#t=" in r.json["url"]
    assert sessions.verify_token(r.json["url"].split("#t=")[1]) == UID
    sessions.revoke_all(UID)
    assert c.get("/api/transactions", headers=auth).status_code == 401
