import asyncio
import base64
import hashlib
import hmac
import smtplib
import time
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import quote, urlparse

import httpx


QQ_TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
QQ_API_BASE = "https://api.bot.qq.com"
_qq_token_cache: dict[str, tuple[str, float]] = {}


def validate_config(kind: str, config: dict):
    if kind in ("dingtalk", "feishu"):
        url = str(config.get("webhook_url") or "")
        if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
            raise ValueError("Webhook URL must be a valid HTTP or HTTPS URL")
    elif kind == "qq":
        for key in ("app_id", "client_secret"):
            if not str(config.get(key) or "").strip():
                raise ValueError(f"QQ {key} is required")
        if str(config.get("chat_type") or "group") not in ("group", "user"):
            raise ValueError("QQ chat_type must be 'group' or 'user'")
    elif kind == "email":
        for key in ("host", "from_address"):
            if not str(config.get(key) or "").strip():
                raise ValueError(f"Email {key} is required")
        try:
            port = int(config.get("port", 587))
        except (TypeError, ValueError) as exc:
            raise ValueError("Email port must be a number") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Email port must be between 1 and 65535")
        if not valid_email(str(config["from_address"])):
            raise ValueError("Invalid sender email address")
        recipients = config.get("default_recipients") or []
        if not isinstance(recipients, list) or any(not valid_email(str(item)) for item in recipients):
            raise ValueError("Invalid default recipient email address")
        if config.get("security", "starttls") not in ("ssl", "starttls", "none"):
            raise ValueError("Invalid SMTP security mode")
    else:
        raise ValueError(f"Unsupported channel: {kind}")


def valid_email(value: str) -> bool:
    name, address = parseaddr(value)
    return not name and address == value and "@" in address and "\n" not in value and "\r" not in value


async def send(kind: str, config: dict, title: str, body: str, recipients: list[str]):
    if kind == "email":
        await asyncio.to_thread(send_email, config, title, body, recipients)
    elif kind == "dingtalk":
        await send_dingtalk(config, title, body)
    elif kind == "feishu":
        await send_feishu(config, title, body)
    elif kind == "qq":
        await send_qq(config, title, body)
    else:
        raise ValueError(f"Unsupported channel: {kind}")


async def send_dingtalk(config: dict, title: str, body: str):
    url = config["webhook_url"]
    if config.get("secret"):
        timestamp = str(int(time.time() * 1000))
        signature = base64.b64encode(hmac.new(config["secret"].encode(),
            f"{timestamp}\n{config['secret']}".encode(), hashlib.sha256).digest()).decode()
        separator = "&" if "?" in url else "?"
        url += f"{separator}timestamp={timestamp}&sign={quote(signature, safe='')}"
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{body}"}}
    await post_webhook(url, payload, "dingtalk")


async def send_feishu(config: dict, title: str, body: str):
    payload = {"msg_type": "text", "content": {"text": f"{title}\n{body}"}}
    if config.get("secret"):
        timestamp = str(int(time.time()))
        key = f"{timestamp}\n{config['secret']}".encode()
        signature = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
        payload.update({"timestamp": timestamp, "sign": signature})
    await post_webhook(config["webhook_url"], payload, "feishu")


async def get_qq_access_token(config: dict) -> str:
    app_id = str(config["app_id"]).strip()
    client_secret = str(config["client_secret"]).strip()
    cache_key = hashlib.sha256(f"{app_id}\0{client_secret}".encode()).hexdigest()
    cached = _qq_token_cache.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(QQ_TOKEN_URL, json={"appId": app_id, "clientSecret": client_secret})
    except httpx.RequestError as exc:
        raise RuntimeError(f"QQ token network error ({type(exc).__name__})") from exc
    result = _json_response(response, "QQ token")
    if response.status_code >= 400 or not result.get("access_token"):
        code = result.get("code", result.get("err_code", response.status_code))
        message = result.get("message") or result.get("msg") or "authentication failed"
        raise RuntimeError(f"QQ token rejected ({code}): {message}")
    try:
        expires_in = max(60, int(result.get("expires_in", 7200)))
    except (TypeError, ValueError):
        expires_in = 7200
    token = str(result["access_token"])
    _qq_token_cache[cache_key] = (token, time.time() + expires_in)
    return token


async def send_qq(config: dict, title: str, body: str):
    target_openid = str(config.get("group_openid") or "").strip()
    if not target_openid:
        raise RuntimeError("QQ channel has no target OpenID; it only identifies the bot")
    token = await get_qq_access_token(config)
    target_openid = quote(target_openid, safe="")
    scope = "users" if str(config.get("chat_type") or "group") == "user" else "groups"
    url = f"{QQ_API_BASE}/v2/{scope}/{target_openid}/messages"
    payload = {"msg_type": 0, "content": f"{title}\n{body}"}
    # Since QQ shut down bot push messages in 2025-04, the only way to deliver
    # is replying to a recent inbound message; msg_id/msg_seq turn the send
    # into such a passive reply.
    if config.get("msg_id"):
        payload["msg_id"] = str(config["msg_id"])
        payload["msg_seq"] = int(config.get("msg_seq", 1))
    await post_qq_message(url, token, payload)


async def send_qq_reply(config: dict, text: str, msg_id: str, msg_seq: int,
                        keyboard: dict | None = None, markdown_template_id: str = ""):
    """Reply to a group message. Groups only render buttons attached to a
    markdown message, and markdown itself requires a platform-approved
    template, so keyboards are only sent when a template id is configured."""
    token = await get_qq_access_token(config)
    openid = quote(str(config["group_openid"]).strip(), safe="")
    url = f"{QQ_API_BASE}/v2/groups/{openid}/messages"
    payload = {"msg_id": msg_id, "msg_seq": msg_seq}
    if markdown_template_id:
        payload["msg_type"] = 2
        payload["markdown"] = {"custom_template_id": markdown_template_id,
                               "params": [{"key": "content", "values": [text]}]}
        if keyboard:
            payload["keyboard"] = keyboard
    else:
        payload["msg_type"] = 0
        payload["content"] = text
    await post_qq_message(url, token, payload)


async def post_qq_message(url: str, token: str, payload: dict):
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"QQBot {token}"},
                json=payload,
            )
    except httpx.RequestError as exc:
        raise RuntimeError(f"QQ message network error ({type(exc).__name__})") from exc
    result = _json_response(response, "QQ message")
    error_code = result.get("err_code", result.get("code", 0))
    if response.status_code >= 400 or error_code not in (0, None):
        message = result.get("message") or result.get("msg") or "message rejected"
        raise RuntimeError(f"QQ message rejected ({error_code or response.status_code}): {message}")


def _json_response(response: httpx.Response, provider: str) -> dict:
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{provider} returned a non-JSON response") from exc


async def post_webhook(url: str, payload: dict, kind: str):
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(url, json=payload)
    except httpx.RequestError as exc:
        raise RuntimeError(f"Webhook network error ({type(exc).__name__})") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"Webhook HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Webhook returned a non-JSON response") from exc
    code = result.get("errcode") if kind == "dingtalk" else result.get("code", result.get("StatusCode", 0))
    if code != 0:
        message = result.get("errmsg") or result.get("msg") or result.get("StatusMessage") or "unknown error"
        raise RuntimeError(f"Webhook rejected message ({code}): {message}")


def send_email(config: dict, title: str, body: str, recipients: list[str]):
    to = recipients or config.get("default_recipients", [])
    if not to or any(not valid_email(str(item)) for item in to):
        raise ValueError("At least one valid email recipient is required")
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = config["from_address"]
    message["To"] = ", ".join(to)
    message.set_content(body)
    host = config["host"]
    port = int(config.get("port", 587))
    security = config.get("security", "starttls")
    if security not in ("ssl", "starttls", "none"):
        raise ValueError("Invalid SMTP security mode")
    client_type = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    with client_type(host, port, timeout=15) as client:
        if security == "starttls":
            client.starttls()
        if config.get("username"):
            client.login(config["username"], config.get("password", ""))
        client.send_message(message)
