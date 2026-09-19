import asyncio
import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import senders


def test_dingtalk_and_feishu_signed_payloads(monkeypatch):
    captured = []

    async def capture(url, payload, kind):
        captured.append((url, payload, kind))

    monkeypatch.setattr(senders, "post_webhook", capture)
    monkeypatch.setattr(senders.time, "time", lambda: 1700000000)
    asyncio.run(senders.send_dingtalk({"webhook_url":"https://example.com/robot?access_token=x","secret":"SEC123"}, "Alert", "Body"))
    url, payload, kind = captured.pop()
    assert kind == "dingtalk"
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["text"] == "### Alert\n\nBody"
    query = parse_qs(urlparse(url).query)
    expected = base64.b64encode(hmac.new(b"SEC123", b"1700000000000\nSEC123", hashlib.sha256).digest()).decode()
    assert query["sign"] == [expected]
    asyncio.run(senders.send_feishu({"webhook_url":"https://example.com/hook","secret":"secret"}, "Alert", "Body"))
    url, payload, kind = captured.pop()
    assert kind == "feishu"
    assert payload["content"]["text"] == "Alert\nBody"
    assert payload["timestamp"] == "1700000000"
    expected = base64.b64encode(hmac.new(b"1700000000\nsecret", b"", hashlib.sha256).digest()).decode()
    assert payload["sign"] == expected


def test_webhook_failure_does_not_expose_url_token(monkeypatch):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(500, json={"error": "failed"}))
    monkeypatch.setattr(senders.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    with pytest.raises(RuntimeError) as error:
        asyncio.run(senders.post_webhook("https://example.com/hook?access_token=private-token", {}, "dingtalk"))
    assert "Webhook HTTP 500" == str(error.value)
    assert "private-token" not in str(error.value)


def test_qq_gets_token_once_and_sends_group_message(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url == httpx.URL(senders.QQ_TOKEN_URL):
            assert request.method == "POST"
            assert request.content == b'{"appId":"123456","clientSecret":"private-secret"}'
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": "7200"})
        assert request.url == httpx.URL(f"{senders.QQ_API_BASE}/v2/groups/group-openid/messages")
        assert request.headers["Authorization"] == "QQBot access-token"
        assert request.content == b'{"msg_type":0,"content":"Alert\\nBody"}'
        return httpx.Response(200, json={"id": "qq-message-id", "timestamp": "2026-09-16T12:00:00+08:00"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(senders.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    senders._qq_token_cache.clear()
    config = {"app_id": "123456", "client_secret": "private-secret", "group_openid": "group-openid"}
    asyncio.run(senders.send_qq(config, "Alert", "Body"))
    asyncio.run(senders.send_qq(config, "Alert", "Body"))
    assert [request.url for request in requests].count(httpx.URL(senders.QQ_TOKEN_URL)) == 1
    assert len(requests) == 3


def test_qq_private_chat_posts_to_users_endpoint(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url == httpx.URL(senders.QQ_TOKEN_URL):
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 7200})
        assert request.url == httpx.URL(f"{senders.QQ_API_BASE}/v2/users/friendly-user-openid/messages")
        return httpx.Response(200, json={"id": "qq-message-id"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(senders.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    senders._qq_token_cache.clear()
    config = {"app_id": "123456", "client_secret": "private-secret", "group_openid": "friendly-user-openid", "chat_type": "user"}
    asyncio.run(senders.send_qq(config, "Alert", "Body"))
    assert str(requests[-1].url).endswith("/v2/users/friendly-user-openid/messages")


def test_qq_passive_reply_includes_msg_id_and_seq(monkeypatch):
    bodies = []

    def handler(request):
        if request.url == httpx.URL(senders.QQ_TOKEN_URL):
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 7200})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "qq-message-id"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(senders.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    senders._qq_token_cache.clear()
    config = {"app_id": "123456", "client_secret": "private-secret", "group_openid": "g", "msg_id": "evt-123", "msg_seq": 2}
    asyncio.run(senders.send_qq(config, "Alert", "Body"))
    assert bodies[-1] == {"msg_type": 0, "content": "Alert\nBody", "msg_id": "evt-123", "msg_seq": 2}
    config.pop("msg_id")
    asyncio.run(senders.send_qq(config, "Alert", "Body"))
    assert bodies[-1] == {"msg_type": 0, "content": "Alert\nBody"}


def test_qq_config_validation_for_chat_type():
    base = {"app_id": "123456", "client_secret": "private-secret", "group_openid": "group-openid"}
    senders.validate_config("qq", base)
    senders.validate_config("qq", {**base, "chat_type": "user"})
    senders.validate_config("qq", {**base, "group_openid": ""})  # identity channel
    with pytest.raises(ValueError, match="chat_type"):
        senders.validate_config("qq", {**base, "chat_type": "guild"})


def test_qq_identity_channel_cannot_send():
    with pytest.raises(RuntimeError, match="no target OpenID"):
        asyncio.run(senders.send_qq({"app_id": "1", "client_secret": "s", "group_openid": ""}, "Alert", "Body"))


def test_qq_failure_does_not_expose_credentials(monkeypatch):
    def handler(request):
        if request.url == httpx.URL(senders.QQ_TOKEN_URL):
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 7200})
        return httpx.Response(400, json={"err_code": 40034105, "message": "机器人非群成员"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(senders.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    senders._qq_token_cache.clear()
    config = {"app_id": "123456", "client_secret": "private-secret", "group_openid": "group-openid"}
    with pytest.raises(RuntimeError) as error:
        asyncio.run(senders.send_qq(config, "Alert", "Body"))
    assert "40034105" in str(error.value)
    assert "private-secret" not in str(error.value)
    assert "access-token" not in str(error.value)
