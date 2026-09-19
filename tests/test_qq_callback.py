import json

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.qq_callback import key_from_secret
from app.settings import Settings


ADMIN = "admin-token-for-testing-very-long-value"
API = "message-api-key-for-testing-very-long-value"
SECRET = "private-qq-bot-secret"


def make_app(tmp_path):
    settings = Settings(ADMIN, API, Fernet.generate_key(), str(tmp_path / "messages.db"))
    return create_app(settings, start_worker=False)


def admin_headers():
    return {"Authorization": f"Bearer {ADMIN}"}


def add_identity_channel(client, app_id="123456", secret=SECRET):
    response = client.post("/api/admin/channels", headers=admin_headers(), json={
        "name": "QQ 机器人", "kind": "qq", "enabled": True,
        "config": {"app_id": app_id, "client_secret": secret, "group_openid": ""},
    })
    assert response.status_code == 201, response.text
    return response.json()


def signed_headers(secret, body: bytes, timestamp="1725442341"):
    signature = key_from_secret(secret).sign(timestamp.encode() + body).hex()
    return {"X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp}


def event_body(event_type: str, data: dict) -> bytes:
    return json.dumps({"op": 0, "id": "event-id", "t": event_type, "d": data}).encode()


def channel_configs(app):
    with app.state.db.connect() as conn:
        return [app.state.db._decode(row) for row in conn.execute("SELECT * FROM channels WHERE kind='qq'")]


def post_event(client, event_type, data, app_id="123456", secret=SECRET):
    body = event_body(event_type, data)
    return client.post(f"/api/qq/callback/{app_id}", content=body, headers=signed_headers(secret, body))


def listed_events(client):
    return client.get("/api/admin/qq/events", headers=admin_headers()).json()["items"]


def test_validation_handshake_matches_official_doc_example(tmp_path):
    # Vector from the official Webhook doc: Bot Secret, plain_token and
    # event_ts must produce exactly the documented signature.
    app = make_app(tmp_path)
    with TestClient(app) as client:
        add_identity_channel(client, app_id="1111111", secret="DG5g3B4j9X2KOErG")
        challenge = json.dumps({"op": 13, "d": {"plain_token": "Arq0D5A61EgUu4OxUvOp", "event_ts": "1725442341"}}).encode()
        response = client.post("/api/qq/callback/1111111", content=challenge,
                               headers={"Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "plain_token": "Arq0D5A61EgUu4OxUvOp",
        "signature": "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01"
                     "bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706",
    }


def test_rejects_requests_when_no_channel_stores_the_secret(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        body = event_body("GROUP_AT_MESSAGE_CREATE", {"group_openid": "new-group"})
        assert client.post("/api/qq/callback/123456", content=body, headers=signed_headers(SECRET, body)).status_code == 403
        challenge = json.dumps({"op": 13, "d": {"plain_token": "tok", "event_ts": "1725442341"}}).encode()
        assert client.post("/api/qq/callback/123456", content=challenge).status_code == 403


def test_group_event_runs_default_channel_slot_and_records_the_event(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        identity = add_identity_channel(client)
        response = post_event(client, "GROUP_AT_MESSAGE_CREATE", {
            "group_openid": "fresh-group-openid",
            "author": {"id": "sender", "member_openid": "sender"},
            "content": " hello bot",
        })
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}
        stored = [config for config in channel_configs(app) if config.get("group_openid")]
        assert stored == [{
            "app_id": "123456", "client_secret": SECRET,
            "chat_type": "group", "group_openid": "fresh-group-openid",
        }]
        listed = client.get("/api/admin/channels", headers=admin_headers()).json()
        created = [item for item in listed if item["name"].startswith("QQ群·")]
        assert len(created) == 1 and created[0]["enabled"] is True
        events = listed_events(client)
        assert len(events) == 1
        assert events[0]["event_type"] == "GROUP_AT_MESSAGE_CREATE"
        assert events[0]["chat_type"] == "group"
        assert events[0]["openid"] == "fresh-group-openid"
        assert events[0]["action"] == "channel"
        assert events[0]["status"] == "ok"
        assert events[0]["detail"].startswith("created channel")
        assert events[0]["payload"]["d"]["content"] == " hello bot"
        # the identity channel itself cannot be used as a send destination
        rejected = client.post("/api/v1/messages", headers={"X-API-Key": API}, json={
            "title": "T", "body": "B", "destinations": [{"channel_id": identity["id"]}],
        })
        assert rejected.status_code == 422
        assert "no target OpenID" in rejected.json()["detail"]


def test_c2c_event_creates_user_channel_and_friend_add_covers_user(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        add_identity_channel(client)
        assert post_event(client, "C2C_MESSAGE_CREATE",
                          {"author": {"id": "a", "user_openid": "friendly-user-openid"}, "content": "hi"}).status_code == 200
        assert post_event(client, "FRIEND_ADD", {"openid": "new-friend"}).status_code == 200
        targets = {(config["chat_type"], config["group_openid"])
                   for config in channel_configs(app) if config.get("group_openid")}
        assert ("user", "friendly-user-openid") in targets
        assert ("user", "new-friend") in targets


def test_duplicate_events_create_a_single_channel(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        add_identity_channel(client)
        for _ in range(3):
            assert post_event(client, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "busy-group"}).status_code == 200
        assert len([c for c in channel_configs(app) if c.get("group_openid") == "busy-group"]) == 1
        events = listed_events(client)
        assert len(events) == 3
        assert events[0]["status"] == "ok"
        assert "already exists" in events[0]["detail"]


def test_bad_or_missing_signature_is_rejected_and_not_recorded(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        add_identity_channel(client)
        body = event_body("GROUP_AT_MESSAGE_CREATE", {"group_openid": "secure-group"})
        assert client.post("/api/qq/callback/123456", content=body,
                           headers=signed_headers("someone-elses-secret", body)).status_code == 401
        assert client.post("/api/qq/callback/123456", content=body).status_code == 401
        assert client.post("/api/qq/callback/123456", content=b"not json").status_code == 400
        assert listed_events(client) == []
        assert all(not config.get("group_openid") for config in channel_configs(app))


def test_forward_route_sends_payload_to_external_slot(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"received": True})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.qq_callback.httpx.AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    with TestClient(app) as client:
        add_identity_channel(client)
        rule = client.post("/api/admin/qq/routes", headers=admin_headers(), json={
            "name": "私聊转发客服", "enabled": True, "event_types": ["C2C_MESSAGE_CREATE"],
            "chat_type": "user", "openid": "", "action": "forward",
            "config": {"webhook_url": "https://care.example.com/qq/events"},
        })
        assert rule.status_code == 201, rule.text
        assert post_event(client, "C2C_MESSAGE_CREATE",
                          {"author": {"id": "a", "user_openid": "care-user"}, "content": "help"}).status_code == 200
        # forwarded to the external slot, no channel created for it
        assert len(captured) == 1
        assert str(captured[0].url) == "https://care.example.com/qq/events"
        assert captured[0].headers["X-QQ-Event"] == "C2C_MESSAGE_CREATE"
        assert captured[0].headers["X-QQ-Appid"] == "123456"
        assert json.loads(captured[0].read()) == json.loads(event_body("C2C_MESSAGE_CREATE", {"author": {"id": "a", "user_openid": "care-user"}, "content": "help"}))
        assert not [c for c in channel_configs(app) if c.get("group_openid") == "care-user"]
        events = listed_events(client)
        assert events[0]["action"] == "forward"
        assert events[0]["status"] == "ok"
        assert "forwarded to care.example.com" in events[0]["detail"]
        # group events still fall through to the default channel slot
        assert post_event(client, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "another-group"}).status_code == 200
        assert len(captured) == 1
        assert [c for c in channel_configs(app) if c.get("group_openid") == "another-group"]


def test_forward_failure_is_recorded_but_always_acked(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(500, json={"error": "boom"}))
    monkeypatch.setattr("app.qq_callback.httpx.AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    with TestClient(app) as client:
        add_identity_channel(client)
        client.post("/api/admin/qq/routes", headers=admin_headers(), json={
            "name": "转发", "enabled": True, "event_types": ["*"], "chat_type": "any",
            "openid": "", "action": "forward", "config": {"webhook_url": "https://down.example.com/hook"},
        })
        response = post_event(client, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "g"})
        assert response.status_code == 200
        events = listed_events(client)
        assert events[0]["status"] == "error"
        assert "HTTP 500" in events[0]["detail"]


def test_route_crud_validation_and_secret_retention(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        default_routes = client.get("/api/admin/qq/routes", headers=admin_headers()).json()
        assert [route["action"] for route in default_routes] == ["channel"]
        assert default_routes[0]["event_types"] == ["*"]
        # forward without a URL is rejected
        invalid = client.post("/api/admin/qq/routes", headers=admin_headers(), json={
            "name": "bad", "enabled": True, "event_types": ["*"], "chat_type": "any",
            "openid": "", "action": "forward", "config": {},
        })
        assert invalid.status_code == 422
        unknown = client.post("/api/admin/qq/routes", headers=admin_headers(), json={
            "name": "bad", "enabled": True, "event_types": ["NOT_A_REAL_EVENT"], "chat_type": "any",
            "openid": "", "action": "log", "config": {},
        })
        assert unknown.status_code == 422
        # stored webhook URL is redacted and kept when the edit leaves it blank
        rule = client.post("/api/admin/qq/routes", headers=admin_headers(), json={
            "name": "转发", "enabled": True, "event_types": ["FRIEND_ADD"], "chat_type": "any",
            "openid": "", "action": "forward", "config": {"webhook_url": "https://slot.example.com/hook?token=abc"},
        }).json()
        assert rule["config"] == {"webhook_url": ""}
        assert rule["configured_secrets"] == ["webhook_url"]
        updated = client.put(f"/api/admin/qq/routes/{rule['id']}", headers=admin_headers(), json={
            "name": "转发改", "enabled": False, "event_types": ["FRIEND_ADD"], "chat_type": "user",
            "openid": "", "action": "forward", "config": {},
        })
        assert updated.status_code == 200
        stored = app.state.db.get_qq_route(rule["id"], include_config=True)
        assert stored["config"]["webhook_url"] == "https://slot.example.com/hook?token=abc"
        assert stored["enabled"] is False and stored["name"] == "转发改"
        assert client.delete(f"/api/admin/qq/routes/{rule['id']}", headers=admin_headers()).json() == {"deleted": True}
        assert client.delete(f"/api/admin/qq/routes/{rule['id']}", headers=admin_headers()).status_code == 404


def test_disabled_rules_fall_back_to_log_only_and_events_can_be_cleared(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        add_identity_channel(client)
        for route in client.get("/api/admin/qq/routes", headers=admin_headers()).json():
            client.put(f"/api/admin/qq/routes/{route['id']}", headers=admin_headers(), json={
                "name": route["name"], "enabled": False, "event_types": route["event_types"],
                "chat_type": route["chat_type"], "openid": route["openid"], "action": route["action"], "config": {},
            })
        assert post_event(client, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "quiet-group"}).status_code == 200
        events = listed_events(client)
        assert len(events) == 1
        assert events[0]["action"] == "log" and events[0]["status"] == "ok"
        assert all(not config.get("group_openid") for config in channel_configs(app))
        assert client.delete("/api/admin/qq/events", headers=admin_headers()).json()["cleared"] == 1
        assert listed_events(client) == []


def test_events_without_target_are_recorded_and_skipped(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        add_identity_channel(client)
        body = event_body("GROUP_MSG_REJECT", {"group_openid": "muted-group"})
        assert client.post("/api/qq/callback/123456", content=body,
                           headers=signed_headers(SECRET, body)).status_code == 200
        heartbeat = json.dumps({"op": 12, "d": 1}).encode()
        assert client.post("/api/qq/callback/123456", content=heartbeat,
                           headers=signed_headers(SECRET, heartbeat)).status_code == 200
        events = listed_events(client)
        assert [event["event_type"] for event in events] == ["GROUP_MSG_REJECT"]
        assert events[0]["action"] == "channel"
        assert "no target openid" in events[0]["detail"]
        assert all(not config.get("group_openid") for config in channel_configs(app))


def test_identity_channel_needs_credentials_but_not_an_openid(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        missing_secret = client.post("/api/admin/channels", headers=admin_headers(), json={
            "name": "no secret", "kind": "qq", "enabled": True,
            "config": {"app_id": "123456", "client_secret": "", "group_openid": ""},
        })
        assert missing_secret.status_code == 422
        assert "client_secret" in missing_secret.json()["detail"]


def test_passive_reply_seq_is_capped_and_window_expires(tmp_path):
    import time as time_module

    app = make_app(tmp_path)
    with TestClient(app) as client:
        add_identity_channel(client)
        assert post_event(client, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "reply-group", "id": "evt-777"}).status_code == 200
        assert app.state.db.latest_qq_interaction_msgid("reply-group", "group") == "evt-777"
        assert app.state.db.latest_qq_interaction_msgid("reply-group", "user") is None
        with app.state.db.connect() as conn:
            conn.execute("UPDATE qq_events SET received_at=? WHERE openid='reply-group'", (time_module.time() - 7200,))
        assert app.state.db.latest_qq_interaction_msgid("reply-group", "group") is None
        assert [app.state.db.claim_qq_reply_seq("evt-777") for _ in range(5)] == [1, 2, 3, 4, None]


def test_worker_delivers_qq_as_passive_reply(tmp_path, monkeypatch):
    import time as time_module

    captured = []

    async def fake_send(kind, config, title, body, recipients):
        captured.append((kind, config))

    monkeypatch.setattr("app.main.send", fake_send)
    settings = Settings(ADMIN, API, Fernet.generate_key(), str(tmp_path / "messages.db"))
    app = create_app(settings, start_worker=True)
    with TestClient(app) as client:
        add_identity_channel(client)
        assert post_event(client, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "notify-group", "id": "evt-999"}).status_code == 200
        channels = client.get("/api/admin/channels", headers=admin_headers()).json()
        target = [item for item in channels if item["config"].get("group_openid") == "notify-group"][0]
        response = client.post("/api/v1/messages", headers={"X-API-Key": API}, json={
            "title": "Alert", "body": "Hello", "destinations": [{"channel_id": target["id"]}],
        })
        assert response.status_code == 202
        deadline = time_module.monotonic() + 3
        while time_module.monotonic() < deadline and not captured:
            time_module.sleep(0.05)
        kind, config = captured.pop()
        assert kind == "qq"
        assert config["msg_id"] == "evt-999"
        assert config["msg_seq"] == 1


def test_party_command_classification():
    from app.qq_party import classify, _extract_name, _extract_time

    assert classify(" 组队 斗地主 ") == "create"
    assert classify("今晚有无一起玩的") == "create"
    assert classify("查询组队") == "query"
    assert classify("谁组队") == "query"
    assert classify("参加 21点") == "join"
    assert classify("上号 20:30") == "join"
    assert classify("+1") == "join"
    assert classify("我不参加了") == "leave"
    assert classify("退出") == "leave"
    assert classify("你好呀") is None
    assert _extract_time("参加 晚上21点半") == "21点半"
    assert _extract_time("上号 20:30") == "20:30"
    assert _extract_name("参加 我是小明 21点") == "小明"


def add_party_route(client, config=None):
    response = client.post("/api/admin/qq/routes", headers=admin_headers(), json={
        "name": "组队游戏插槽", "enabled": True, "event_types": ["GROUP_AT_MESSAGE_CREATE"],
        "chat_type": "group", "openid": "", "action": "party", "config": config or {},
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_party_slot_replies_with_plain_text_without_a_template(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    payloads = []

    def handler(request):
        if request.url == httpx.URL("https://api.bot.qq.com/app/getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "sent"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.senders.httpx.AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    with TestClient(app) as client:
        add_identity_channel(client)
        add_party_route(client)
        assert post_event(client, "GROUP_AT_MESSAGE_CREATE", {
            "group_openid": "game-group", "id": "m1", "content": " 组队 斗地主 ",
            "author": {"id": "member-aaa", "member_openid": "member-aaa"},
        }).status_code == 200
        card = payloads[-1]
        assert card == {"msg_type": 0, "msg_id": "m1", "msg_seq": 1, "content": card["content"]}
        assert "组队游戏邀请" in card["content"] and "已报名 1 人" in card["content"]
        assert "keyboard" not in card
        event = listed_events(client)[0]
        assert event["action"] == "party" and event["status"] == "ok"
        assert "未附按钮" in event["detail"]


def test_party_slot_attaches_buttons_to_markdown_template(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    payloads = []

    def handler(request):
        if request.url == httpx.URL("https://api.bot.qq.com/app/getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "sent"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.senders.httpx.AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    with TestClient(app) as client:
        add_identity_channel(client)
        add_party_route(client, {"markdown_template_id": "tpl-123"})
        assert post_event(client, "GROUP_AT_MESSAGE_CREATE", {
            "group_openid": "game-group", "id": "m2", "content": " 参加 21点 我是小明 ",
            "author": {"id": "member-bbb", "member_openid": "member-bbb"},
        }).status_code == 200
        message = payloads[-1]
        assert message["msg_type"] == 2 and message["msg_id"] == "m2" and message["msg_seq"] == 1
        assert message["markdown"]["custom_template_id"] == "tpl-123"
        assert "小明 · 预计 21点" in message["markdown"]["params"][0]["values"][0]
        join_button = message["keyboard"]["content"]["rows"][0]["buttons"][0]
        assert join_button["action"]["data"] == "参加"
        assert join_button["action"]["permission"] == {"type": 2}
        assert "未附按钮" not in listed_events(client)[0]["detail"]


def test_party_rule_falls_through_for_unrelated_messages(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        add_identity_channel(client)
        add_party_route(client)
        assert post_event(client, "GROUP_AT_MESSAGE_CREATE", {
            "group_openid": "chat-group", "id": "m9", "content": " 你好呀 ",
            "author": {"id": "member-aaa", "member_openid": "member-aaa"},
        }).status_code == 200
        events = listed_events(client)
        assert events[0]["action"] == "channel"
        stored = [config for config in channel_configs(app) if config.get("group_openid") == "chat-group"]
        assert len(stored) == 1
