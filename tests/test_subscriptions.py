import asyncio
import json
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import app.subscriptions as subscription_module

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from app.subscriptions import fetch_rss

ADMIN = "subscription-test-admin-token-long"
HEADERS = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture
def subscription_clock(monkeypatch):
    clock = SimpleNamespace(current=float(int(time.time()) + 5))
    def advance(seconds=60):
        clock.current += seconds
    clock.advance = advance
    clock.time = lambda: clock.current
    monkeypatch.setattr(subscription_module, "time", clock)
    return clock


@pytest.fixture
def app(tmp_path, subscription_clock):
    return create_app(Settings(ADMIN, "subscription-test-api-token-long", Fernet.generate_key(),
                               str(tmp_path / "test.db")), start_worker=False)


def channel(client, kind="feishu", config=None):
    return client.post("/api/admin/channels", headers=HEADERS, json={
        "name": kind, "kind": kind, "config": config or {"webhook_url": "https://example.com/hook"},
    }).json()


def subscription(client, channels, kind="rss", config=None):
    payload = {"name": "技术动态", "kind": kind, "config": config or {"feed_url": "https://example.com/feed?key=private"},
               "destinations": [{"channel_id": item["id"]} for item in channels]}
    response = client.post("/api/admin/subscriptions", headers=HEADERS, json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload


def rss(ids):
    return '<rss version="2.0"><channel><title>News</title>' + ''.join(
        f'<item><pubDate>{format_datetime(datetime.fromtimestamp(subscription_module.time.time(), timezone.utc))}</pubDate><guid>{item}</guid><title>标题 {item}</title><description>&lt;p&gt;正文 &amp;amp; 更新&lt;/p&gt;</description><link>https://example.com/{item}</link></item>'
        for item in ids) + '</channel></rss>'


def x_post(values):
    return {"created_at": datetime.fromtimestamp(subscription_module.time.time(), timezone.utc).isoformat(), **values}


def mock_http(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.subscriptions.httpx.AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler)))


def check(client, item):
    subscription_module.time.advance()
    response = client.post(f"/api/admin/subscriptions/{item['id']}/check", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def test_rss_baseline_multiple_channels_dedup_and_restart(app, monkeypatch):
    current = {"ids": ["old"]}
    mock_http(monkeypatch, lambda request: httpx.Response(200, text=rss(current["ids"])))
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client), channel(client, "dingtalk")])
        assert check(client, item)["baseline"] is True
        assert app.state.db.stats()["messages"] == 0
        current["ids"] = ["new", "old"]
        assert check(client, item)["queued"] == 1
        assert check(client, item)["queued"] == 0
        message = app.state.db.list_messages()["items"][0]
        assert len(message["deliveries"]) == 2
        assert message["body"].startswith("正文 & 更新\n\n发布时间：")
        assert message["body"].endswith("https://example.com/new")
        assert message["source"] == f"subscription:{item['id']}"
    restarted = create_app(app.state.settings, start_worker=False)
    with TestClient(restarted) as client:
        assert check(client, item)["queued"] == 0
        current["ids"] = ["newer", "new", "old"]
        assert check(client, item)["queued"] == 1


def test_auth_redaction_validation_edit_and_delete(app):
    with TestClient(app) as client:
        assert client.get("/api/admin/subscriptions").status_code == 401
        assert client.post("/api/admin/subscriptions", json={}).status_code == 401
        target = channel(client)
        item, payload = subscription(client, [target], "x", {
            "username": "https://x.com/Example", "bearer_token": "private-test-token"})
        assert item["config"]["username"] == "example"
        assert item["config"]["bearer_token"] == ""
        assert "state" not in item
        assert "private-test-token" not in client.get("/api/admin/subscriptions", headers=HEADERS).text
        assert b"private-test-token" not in open(app.state.db.path, "rb").read()
        response = client.put(f"/api/admin/subscriptions/{item['id']}", headers=HEADERS,
                              json={**payload, "config": {"username": "@example", "bearer_token": ""}})
        assert response.status_code == 200
        assert app.state.db.get_subscription(item["id"], True)["config"]["bearer_token"] == "private-test-token"
        assert client.delete(f"/api/admin/channels/{target['id']}", headers=HEADERS).status_code == 409
        for bad in ({"poll_interval": 59}, {"destinations": []},
                    {"destinations": [{"channel_id": target["id"]}] * 2},
                    {"destinations": [{"channel_id": "missing"}]},
                    {"config": {"username": "https://evil.example/account", "bearer_token": "token"}},
                    {"config": {"username": "a", "bearer_token": "token", "include_replies": "false"}}):
            assert client.post("/api/admin/subscriptions", headers=HEADERS, json={**payload, **bad}).status_code == 422
        assert client.post(f"/api/admin/subscriptions/{item['id']}/check").status_code == 401
        assert client.delete(f"/api/admin/subscriptions/{item['id']}", headers=HEADERS).status_code == 200
        assert client.post(f"/api/admin/subscriptions/{item['id']}/check", headers=HEADERS).status_code == 404
        assert client.delete(f"/api/admin/channels/{target['id']}", headers=HEADERS).status_code == 200


def test_invalid_feed_and_errors_never_advance_or_expose_secrets(app, monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, text="<html>Login</html>"))
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)])
        assert check(client, item)["error"]
        assert app.state.db.get_subscription(item["id"], True)["initialized"] is False
        assert "private" not in json.dumps(check(client, item))


def test_atomic_delivery_failure_retries_without_losing_entries(app, monkeypatch):
    current = {"ids": ["old"]}
    mock_http(monkeypatch, lambda request: httpx.Response(200, text=rss(current["ids"]), headers={"etag": str(current["ids"])}))
    with TestClient(app) as client:
        target = channel(client)
        item, _ = subscription(client, [target])
        check(client, item)
        before = app.state.db.get_subscription(item["id"], True)["state"]
        current["ids"] = ["new", "old"]
        with app.state.db.connect() as conn:
            conn.execute("UPDATE channels SET enabled=0 WHERE id=?", (target["id"],))
        assert check(client, item)["error"]
        assert app.state.db.get_subscription(item["id"], True)["state"] == before
        assert app.state.db.stats()["messages"] == 0
        with app.state.db.connect() as conn:
            conn.execute("UPDATE channels SET enabled=1 WHERE id=?", (target["id"],))
        assert check(client, item)["queued"] == 1


def test_atom_conditional_get_and_empty_feed():
    requests = []
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title><entry><id>urn:1</id><title>Atom title</title><link href="https://example.com/atom"/><content type="html">&lt;p&gt;Atom body&lt;/p&gt;</content></entry></feed>'
    def handler(request):
        requests.append(request)
        return httpx.Response(304) if len(requests) == 2 else httpx.Response(200, text=atom, headers={"etag": '"v1"'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = {"name": "Atom", "state": {}, "config": {"feed_url": "https://example.com/atom"}}
            entries, state = await fetch_rss(client, item)
            assert entries[0]["body"] == "Atom body\n\nhttps://example.com/atom"
            item["state"] = state
            assert (await fetch_rss(client, item))[0] == []
    asyncio.run(run())
    assert requests[1].headers["if-none-match"] == '"v1"'


def test_empty_feed_valid_but_broken_x_response_does_not_establish_baseline(app, monkeypatch):
    current = {"broken": False}
    def handler(request):
        if "feed" in request.url.path:
            return httpx.Response(200, text=rss([]))
        if "/by/username/" in request.url.path:
            return httpx.Response(200, json={"data": {"id": "123"}})
        return httpx.Response(200, json={} if current["broken"] else {"meta": {"result_count": 0}})
    mock_http(monkeypatch, handler)
    with TestClient(app) as client:
        target = channel(client)
        rss_item, _ = subscription(client, [target])
        assert check(client, rss_item)["baseline"]
        x_item, _ = subscription(client, [target], "x", {"username": "author", "bearer_token": "token"})
        current["broken"] = True
        assert check(client, x_item)["error"]
        assert not app.state.db.get_subscription(x_item["id"])["initialized"]
        current["broken"] = False
        assert check(client, x_item)["baseline"]


def test_x_lookup_time_window_pagination_filters_and_order(app, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer private-token"
        if "/by/username/" in request.url.path:
            return httpx.Response(200, json={"data": {"id": "123"}})
        if "start_time" not in request.url.params:
            assert request.url.params["max_results"] == "5"
            return httpx.Response(200, json={"data": [x_post({"id": "10", "text": "old"})], "meta": {"next_token": "ignored"}})
        assert "start_time" in request.url.params and "end_time" in request.url.params
        assert "since_id" not in request.url.params
        assert request.url.params["exclude"] == "replies,retweets"
        if "pagination_token" not in request.url.params:
            return httpx.Response(200, json={"data": [x_post({"id": "12", "text": "latest"})], "meta": {"next_token": "page2"}})
        return httpx.Response(200, json={"data": [x_post({"id": "11", "text": "next https://t.co/a", "entities": {
            "urls": [{"url": "https://t.co/a", "expanded_url": "https://example.com/article"}]}})]})
    mock_http(monkeypatch, handler)
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "x", {"username": "@author", "bearer_token": "private-token"})
        assert check(client, item)["baseline"]
        assert check(client, item)["queued"] == 2
        assert len(requests) == 4  # cached numeric user ID; initial baseline is one page
        assert app.state.db.get_subscription(item["id"], True)["last_success_at"] == subscription_module.time.time()
        messages = app.state.db.list_messages()["items"]
        assert {message["idempotency_key"] for message in messages} == {"11", "12"}
        assert any("https://example.com/article" in message["body"] for message in messages)


def test_x_partial_page_error_and_rate_limit_keep_cursor(app, monkeypatch):
    current = {"mode": "baseline"}
    def handler(request):
        if "/by/username/" in request.url.path:
            return httpx.Response(200, json={"data": {"id": "123"}})
        if current["mode"] == "baseline":
            return httpx.Response(200, json={"data": [x_post({"id": "10", "text": "old"})]})
        if current["mode"] == "limited":
            return httpx.Response(429, text="private-token", headers={"retry-after": "600"})
        if "pagination_token" in request.url.params:
            return httpx.Response(401, text="private-token")
        return httpx.Response(200, json={"data": [x_post({"id": "11", "text": "new"})], "meta": {"next_token": "page2"}})
    mock_http(monkeypatch, handler)
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "x", {"username": "author", "bearer_token": "private-token"})
        check(client, item)
        before = app.state.db.get_subscription(item["id"])["last_success_at"]
        current["mode"] = "partial"
        assert "401" in check(client, item)["error"]
        assert app.state.db.get_subscription(item["id"], True)["last_success_at"] == before
        assert app.state.db.stats()["messages"] == 0
        current["mode"] = "limited"
        result = check(client, item)
        assert "429" in result["error"]
        assert "private-token" not in json.dumps(result)
        assert result["subscription"]["next_poll_at"] > time.time() + 590
        assert client.post(f"/api/admin/subscriptions/{item['id']}/check", headers=HEADERS).status_code == 429


def test_edit_during_fetch_discards_stale_results_and_source_change_resets(app, monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, text=rss(["old"])))
    with TestClient(app) as client:
        item, payload = subscription(client, [channel(client)])
        check(client, item)
        stale = app.state.db.get_subscription(item["id"], True)
        response = client.put(f"/api/admin/subscriptions/{item['id']}", headers=HEADERS,
                              json={**payload, "name": "renamed", "config": {"feed_url": ""}})
        assert response.json()["initialized"]
        assert app.state.db.apply_subscription_entries(stale, [{"id": "new", "title": "t", "body": "b"}], {}) == 0
        response = client.put(f"/api/admin/subscriptions/{item['id']}", headers=HEADERS,
                              json={**payload, "config": {"feed_url": "https://example.com/other"}})
        assert response.json()["initialized"] is False
        assert check(client, item)["baseline"]
        assert client.put(f"/api/admin/subscriptions/{item['id']}", headers=HEADERS,
                          json={**payload, "enabled": False}).status_code == 200
        assert client.post(f"/api/admin/subscriptions/{item['id']}/check", headers=HEADERS).status_code == 409


def test_background_scheduler_feeds_existing_delivery_worker(tmp_path, monkeypatch, subscription_clock):
    current = {"ids": ["old"]}
    delivered = []
    mock_http(monkeypatch, lambda request: httpx.Response(200, text=rss(current["ids"])))
    async def send(*args):
        delivered.append(args)
    monkeypatch.setattr("app.main.send", send)
    app = create_app(Settings(ADMIN, "subscription-test-api-token-long", Fernet.generate_key(),
                              str(tmp_path / "worker.db")))
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)])
        deadline = time.monotonic() + 5
        while not app.state.db.get_subscription(item["id"])["initialized"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        current["ids"] = ["new", "old"]
        subscription_clock.advance()
        with app.state.db.connect() as conn:
            conn.execute("UPDATE subscriptions SET next_poll_at=0 WHERE id=?", (item["id"],))
        while not delivered or app.state.db.stats()["sent"] != 1:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert delivered[0][2] == "标题 new"


def dated_rss(entries):
    return '<rss version="2.0"><channel><title>News</title>' + ''.join(
        f'<item><guid>{identity}</guid><title>{identity}</title>'
        + (f'<pubDate>{format_datetime(datetime.fromtimestamp(timestamp, timezone.utc))}</pubDate>' if timestamp is not None else '')
        + '</item>' for identity, timestamp in entries) + '</channel></rss>'


def test_rss_time_window_skips_old_and_undated_and_defers_future(app, monkeypatch):
    current = {"entries": [], "etag": '"v1"'}
    requests = []
    def handler(request):
        requests.append(request)
        if request.headers.get("if-none-match") == current["etag"]:
            return httpx.Response(304)
        return httpx.Response(200, text=dated_rss(current["entries"]), headers={"etag": current["etag"]})
    mock_http(monkeypatch, handler)
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)])
        check(client, item)
        start = app.state.db.get_subscription(item["id"])["last_success_at"]
        current.update(entries=[("old", start - 10), ("boundary", start), ("now", start + 60),
                                ("future", start + 61), ("undated", None)], etag='"v2"')
        assert check(client, item)["queued"] == 1
        assert app.state.db.list_messages()["items"][0]["title"] == "now"
        assert app.state.db.get_subscription(item["id"], True)["state"] == {}
        assert check(client, item)["queued"] == 1
        assert "if-none-match" not in requests[-1].headers
        assert {message["title"] for message in app.state.db.list_messages()["items"]} == {"now", "future"}
        assert check(client, item)["queued"] == 0  # 304 still advances the successful time.


def test_check_completion_time_does_not_skip_next_interval(app, monkeypatch):
    current = {"entries": []}
    def handler(request):
        if not current["entries"]:
            # An item published during the initial network request is next-window content.
            current["entries"] = [("during-fetch", subscription_module.time.time() + 5)]
            subscription_module.time.advance(10)
        return httpx.Response(200, text=dated_rss(current["entries"]), headers={"etag": '"same"'})
    mock_http(monkeypatch, handler)
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)])
        beginning = subscription_module.time.time() + 60
        result = check(client, item)
        assert result["baseline"] and result["queued"] == 0
        assert result["subscription"]["last_success_at"] == beginning
        assert result["subscription"]["last_checked_at"] == beginning
        assert check(client, item)["queued"] == 1


def test_x_uses_fixed_time_bounds_and_does_not_skip_future_ids(app, monkeypatch):
    current = {"posts": []}
    requests = []
    def handler(request):
        requests.append(request)
        if "/by/username/" in request.url.path:
            return httpx.Response(200, json={"data": {"id": "123"}})
        return httpx.Response(200, json={"data": current["posts"], "meta": {"result_count": len(current["posts"])}})
    mock_http(monkeypatch, handler)
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "x", {"username": "author", "bearer_token": "token"})
        check(client, item)
        start = app.state.db.get_subscription(item["id"])["last_success_at"]
        current["posts"] = [x_post({"id": str(i), "text": "post", "created_at": datetime.fromtimestamp(start + delta, timezone.utc).isoformat()})
                            for i, delta in ((11, -1), (12, 0), (13, 60), (14, 61))]
        assert check(client, item)["queued"] == 1
        assert "since_id" not in requests[-1].url.params
        assert requests[-1].url.params["start_time"] == datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        assert requests[-1].url.params["end_time"] == datetime.fromtimestamp(start + 60, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        assert check(client, item)["queued"] == 1
        assert {message["idempotency_key"] for message in app.state.db.list_messages()["items"]} == {"13", "14"}


def test_legacy_subscription_migration_uses_existing_check_time(app):
    from app.database import Database
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)])
        with app.state.db.connect() as conn:
            conn.execute("UPDATE subscriptions SET initialized=1,last_checked_at=12345 WHERE id=?", (item["id"],))
            conn.execute("ALTER TABLE subscriptions DROP COLUMN last_success_at")
        migrated = Database(app.state.db.path, app.state.settings.encryption_key)
        assert migrated.get_subscription(item["id"])["last_success_at"] == 12345
        # A later startup must not overwrite the successful bound with a failed attempt.
        with migrated.connect() as conn:
            conn.execute("UPDATE subscriptions SET last_checked_at=23456 WHERE id=?", (item["id"],))
        restarted = Database(app.state.db.path, app.state.settings.encryption_key)
        assert restarted.get_subscription(item["id"])["last_success_at"] == 12345
