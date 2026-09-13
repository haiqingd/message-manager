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


def validate_config(kind: str, config: dict):
    if kind in ("dingtalk", "feishu"):
        url = str(config.get("webhook_url") or "")
        if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
            raise ValueError("Webhook URL must be a valid HTTP or HTTPS URL")
    else:
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
