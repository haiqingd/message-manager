import asyncio
import base64
import hashlib
import hmac
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
