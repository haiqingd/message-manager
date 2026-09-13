import json
import sqlite3
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings


ADMIN = "admin-token-for-testing-very-long-value"
API = "message-api-key-for-testing-very-long-value"


def make_app(tmp_path, worker=False):
    settings = Settings(ADMIN, API, Fernet.generate_key(), str(tmp_path / "messages.db"))
    return create_app(settings, start_worker=worker)


def admin_headers():
    return {"Authorization": f"Bearer {ADMIN}"}


def api_headers():
    return {"X-API-Key": API}


def add_channel(client, kind="dingtalk", config=None):
    config = config or {"webhook_url": "https://example.com/robot?access_token=secret", "secret": "my-signing-secret"}
    response = client.post("/api/admin/channels", headers=admin_headers(), json={"name": "Ops", "kind": kind, "enabled": True, "config": config})
    assert response.status_code == 201, response.text
    return response.json()


def test_auth_config_redaction_and_update(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/admin/channels").status_code == 401
        assert client.post("/api/v1/messages", json={}).status_code == 401
        channel = add_channel(client)
        assert channel["config"]["webhook_url"] == ""
        assert channel["config"]["secret"] == ""
        assert set(channel["configured_secrets"]) == {"webhook_url", "secret"}
        assert channel["webhook_host"] == "example.com"
        updated = client.put(f"/api/admin/channels/{channel['id']}", headers=admin_headers(), json={"name":"Ops Updated","kind":"dingtalk","enabled":True,"config":{"webhook_url":"","secret":""}})
        assert updated.status_code == 200
        stored = app.state.db.get_channel(channel["id"], include_config=True)
        assert stored["config"]["webhook_url"].startswith("https://example.com")
        assert stored["config"]["secret"] == "my-signing-secret"
        assert b"my-signing-secret" not in (tmp_path / "messages.db").read_bytes()


def test_feishu_credentials_are_saved_but_not_echoed(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        channel = add_channel(client, "feishu", {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/private-token", "secret": "private-signing-key"})
        assert channel["webhook_host"] == "open.feishu.cn"
        assert set(channel["configured_secrets"]) == {"webhook_url", "secret"}
        assert channel["config"]["webhook_url"] == ""
        assert channel["config"]["secret"] == ""
        saved = app.state.db.get_channel(channel["id"], include_config=True)
        assert saved["config"]["webhook_url"].endswith("private-token")
        assert saved["config"]["secret"] == "private-signing-key"


def test_queue_idempotency_and_validation(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        channel = add_channel(client)
        payload = {"title":"Alert","body":"Service recovered","destinations":[{"channel_id":channel["id"]}],"idempotency_key":"event-1"}
        first = client.post("/api/v1/messages", headers=api_headers(), json=payload)
        second = client.post("/api/v1/messages", headers=api_headers(), json=payload)
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert second.json()["created"] is False
        other_source = client.post("/api/v1/messages", headers=api_headers(), json={**payload, "source": "another-app"})
        assert other_source.status_code == 202
        assert other_source.json()["id"] != first.json()["id"]
        detail = client.get(f"/api/v1/messages/{first.json()['id']}", headers=api_headers())
        assert detail.status_code == 200
        assert detail.json()["deliveries"][0]["status"] == "pending"
        admin_detail = client.get(f"/api/admin/messages/{first.json()['id']}", headers=admin_headers())
        assert admin_detail.status_code == 200
        assert admin_detail.json()["id"] == first.json()["id"]
        assert client.get(f"/api/admin/messages/{first.json()['id']}").status_code == 401
        assert client.post("/api/v1/messages", headers=api_headers(), json={**payload,"destinations":[{"channel_id":"missing"}],"idempotency_key":"event-2"}).status_code == 422
        assert app.state.db.stats()["messages"] == 2


def test_legacy_database_column_order_accepts_message(tmp_path):
    path = tmp_path / "messages.db"
    key = Fernet.generate_key()
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE messages (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
            idempotency_key TEXT UNIQUE, created_at REAL NOT NULL
        )""")
        conn.execute("INSERT INTO messages VALUES (?,?,?,?,?)", ("old-id", "Previous", "Saved message", "shared-key", 123.0))
        conn.execute("""CREATE TABLE channels (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            enabled INTEGER NOT NULL, config_encrypted BLOB NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""")
        encrypted = Fernet(key).encrypt(json.dumps({"webhook_url": "https://example.com/hook"}).encode())
        conn.execute("INSERT INTO channels VALUES (?,?,?,?,?,?,?)", ("old-channel", "Previous channel", "feishu", 1, encrypted, 123.0, 123.0))
        conn.execute("""CREATE TABLE deliveries (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
            channel_id TEXT NOT NULL REFERENCES channels(id), recipients_json TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3, next_attempt_at REAL NOT NULL,
            last_error TEXT, sent_at REAL, created_at REAL NOT NULL
        )""")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("old-delivery", "old-id", "old-channel", "[]", "sent", 1, 3, 123.0, None, 124.0, 123.0))
    app = create_app(Settings(ADMIN, API, key, str(path)), start_worker=False)
    with TestClient(app) as client:
        channel = add_channel(client, "feishu")
        response = client.post("/api/admin/messages", headers=admin_headers(), json={
            "source": "console", "title": "Legacy check", "body": "Hello",
            "destinations": [{"channel_id": channel["id"]}], "idempotency_key": "shared-key",
        })
        assert response.status_code == 202, response.text
        previous = app.state.db.get_message("old-id")
        assert previous["title"] == "Previous"
        assert previous["deliveries"][0]["status"] == "sent"
        stored = app.state.db.get_message(response.json()["id"])
        assert stored["source"] == "console"
        assert stored["title"] == "Legacy check"
        assert stored["body"] == "Hello"
        assert stored["created_at"] > 0


def test_worker_sends_and_failed_delivery_can_retry(tmp_path, monkeypatch):
    calls = []
    fail = {"value": True}

    async def fake_send(kind, config, title, body, recipients):
        calls.append((kind, title, body))
        if fail["value"]:
            raise RuntimeError("simulated provider failure")

    monkeypatch.setattr("app.main.send", fake_send)
    app = make_app(tmp_path, worker=True)
    with TestClient(app) as client:
        channel = add_channel(client)
        payload = {"title":"Alert","body":"Check status","destinations":[{"channel_id":channel["id"]}]}
        response = client.post("/api/v1/messages", headers=api_headers(), json=payload)
        assert response.status_code == 202
        message_id = response.json()["id"]
        delivery_id = response.json()["deliveries"][0]["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if app.state.db.get_message(message_id)["deliveries"][0]["attempts"]:
                break
            time.sleep(0.05)
        assert app.state.db.get_message(message_id)["deliveries"][0]["status"] == "pending"
        assert calls == [("dingtalk", "Alert", "Check status")]
        with app.state.db.connect() as conn:
            conn.execute("UPDATE deliveries SET status='failed', attempts=3 WHERE id=?", (delivery_id,))
        fail["value"] = False
        retry = client.post(f"/api/admin/deliveries/{delivery_id}/retry", headers=admin_headers())
        assert retry.status_code == 200
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if app.state.db.get_message(message_id)["deliveries"][0]["status"] == "sent":
                break
            time.sleep(0.05)
        assert app.state.db.get_message(message_id)["deliveries"][0]["status"] == "sent"


def test_email_requires_recipients(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        channel = add_channel(client, "email", {"host":"smtp.example.com","port":587,"security":"starttls","from_address":"notify@example.com","username":"","password":"","default_recipients":[]})
        response = client.post("/api/v1/messages", headers=api_headers(), json={"title":"Test","body":"Hello","destinations":[{"channel_id":channel["id"]}]})
        assert response.status_code == 422
        response = client.post("/api/v1/messages", headers=api_headers(), json={"title":"Test","body":"Hello","destinations":[{"channel_id":channel["id"],"recipients":["person@example.com"]}]})
        assert response.status_code == 202
