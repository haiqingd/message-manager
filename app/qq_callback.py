"""QQ bot webhook callback with slot routing.

The QQ open platform pushes bot events (group @-messages, C2C private
messages, add-to-group, friend add) to the HTTPS callback URL configured
in the console. Signature keys are derived from the bot's AppSecret per
the official docs (repeat the secret to 32 bytes and use it as the
Ed25519 seed); the secret is taken from any stored QQ channel for the
AppID in the callback URL, so no key pair or .env entry is needed —
create a QQ channel with just the AppID and AppSecret to get started.

Every event is recorded in qq_events (the fallback slot, visible in the
admin UI). Routing rules then decide, per event, which slot handles it:
create a send channel in this app, forward the payload to an external
webhook, or just keep the record. Rules are managed from the admin UI;
unmatched events fall through to record-only.
"""

import json
import logging
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .database import Database
from .models import ChannelInput, RouteInput
from . import qq_party


logger = logging.getLogger(__name__)

GROUP_EVENTS = ("GROUP_AT_MESSAGE_CREATE", "GROUP_ADD_ROBOT")
USER_EVENTS = ("C2C_MESSAGE_CREATE", "FRIEND_ADD")

ROUTE_EVENT_TYPES = ("*",) + GROUP_EVENTS + USER_EVENTS
ROUTE_ACTIONS = ("log", "channel", "forward", "party")


class CallbackRejected(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def key_from_secret(bot_secret: str) -> Ed25519PrivateKey:
    """Derive the Ed25519 key the platform signs with: repeat the Bot
    Secret (AppSecret) until it reaches the 32-byte seed size."""
    seed = bot_secret.encode()
    while len(seed) < 32:
        seed *= 2
    return Ed25519PrivateKey.from_private_bytes(seed[:32])


def validate_route(data: RouteInput, config: dict):
    unknown = set(data.event_types) - set(ROUTE_EVENT_TYPES)
    if unknown:
        raise ValueError(f"Unknown QQ event types: {', '.join(sorted(unknown))}")
    if data.action == "forward" and not str(config.get("webhook_url") or "").strip():
        raise ValueError("Forward routes need a slot webhook URL")
    if data.openid and not str(data.openid).strip():
        raise ValueError("Route openid cannot be blank when set")


def _extract_target(event_type: str, data: dict) -> tuple[str, str] | None:
    """Map an event to (chat_type, openid), or None if not applicable."""
    if event_type in GROUP_EVENTS:
        openid = str(data.get("group_openid") or "").strip()
        return ("group", openid) if openid else None
    if event_type in USER_EVENTS:
        author = data.get("author") or {}
        openid = str(author.get("user_openid") or data.get("user_openid")
                     or author.get("id") or data.get("openid") or "").strip()
        return ("user", openid) if openid else None
    return None


async def handle_callback(db: Database, app_id: str, raw_body: bytes,
                          signature: str = "", timestamp: str = "") -> dict:
    credentials = db.qq_credentials(app_id)
    if not credentials["client_secret"]:
        raise CallbackRejected(403, "No QQ channel stores the AppSecret for this app; create one first")
    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise CallbackRejected(400, "Callback body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CallbackRejected(400, "Callback body must be a JSON object")
    key = key_from_secret(credentials["client_secret"])
    op = payload.get("op")
    if op == 13:
        # URL validation challenge; the answer proves we hold the AppSecret.
        return _answer_validation(key, payload.get("d") or {})
    _verify_signature(key, raw_body, signature, timestamp)
    if op == 0:
        await _process_event(db, app_id, credentials, payload)
    return {"ok": True}


def _answer_validation(key: Ed25519PrivateKey, data: dict) -> dict:
    plain_token = str(data.get("plain_token") or "")
    event_ts = str(data.get("event_ts") or "")
    if not plain_token or not event_ts:
        raise CallbackRejected(422, "Validation payload is missing plain_token/event_ts")
    signature = key.sign(f"{event_ts}{plain_token}".encode()).hex()
    return {"plain_token": plain_token, "signature": signature}


def _verify_signature(key: Ed25519PrivateKey, raw_body: bytes, signature: str, timestamp: str):
    if not signature or not timestamp:
        raise CallbackRejected(401, "Missing signature headers")
    try:
        signature_bytes = bytes.fromhex(signature)
    except ValueError as exc:
        raise CallbackRejected(401, "Signature is not valid hex") from exc
    try:
        key.public_key().verify(signature_bytes, timestamp.encode() + raw_body)
    except (InvalidSignature, ValueError) as exc:
        raise CallbackRejected(401, "Signature verification failed") from exc


async def _process_event(db: Database, app_id: str, credentials: dict, payload: dict):
    event_type = str(payload.get("t") or "")
    data = payload.get("d")
    data = data if isinstance(data, dict) else {}
    target = _extract_target(event_type, data)
    chat_type, openid = target if target else (None, None)
    route = _match_route(db, event_type, chat_type, openid, data)
    action = route["action"] if route else "log"
    event_id = db.record_qq_event(app_id, event_type, chat_type, openid, payload,
                                  route["id"] if route else None, action)
    try:
        detail = await _run_action(db, app_id, credentials, action, route, payload, data, target)
        db.finish_qq_event(event_id, "ok", detail)
    except Exception as exc:
        # Always ACK the platform: slot failures are visible in the event log
        # instead of triggering QQ retries that would repeat side effects.
        db.finish_qq_event(event_id, "error", str(exc))
        logger.warning("QQ callback slot %s failed for %s: %s", action, event_type, exc)


def _match_route(db: Database, event_type: str, chat_type: str | None, openid: str | None, data: dict) -> dict | None:
    for candidate in db.list_qq_routes():
        if not candidate["enabled"]:
            continue
        types = candidate["event_types"]
        if "*" not in types and event_type not in types:
            continue
        if candidate["chat_type"] != "any" and (chat_type is None or candidate["chat_type"] != chat_type):
            continue
        if candidate["openid"] and (openid is None or candidate["openid"] != openid):
            continue
        # The party slot only claims group messages matching one of its
        # commands; everything else falls through to the later rules.
        if candidate["action"] == "party":
            if chat_type != "group" or qq_party.classify(str(data.get("content") or "")) is None:
                continue
        return db.get_qq_route(candidate["id"], include_config=True)
    return None


async def _run_action(db: Database, app_id: str, credentials: dict, action: str,
                      route: dict | None, payload: dict, data: dict,
                      target: tuple[str, str] | None) -> str:
    if action == "channel":
        if not target:
            return "event carries no target openid; skipped"
        chat_type, openid = target
        if db.find_qq_channel(openid, chat_type):
            return f"channel for {openid[:12]}… already exists"
        config = {"app_id": app_id, "client_secret": credentials["client_secret"],
                  "chat_type": chat_type, "group_openid": openid}
        label = "QQ群" if chat_type == "group" else "QQ私聊"
        channel = db.save_channel(ChannelInput(name=f"{label}·{openid[:12]}", kind="qq",
                                               enabled=True, config=config))
        logger.info("QQ callback created channel %s (%s) via route %s", channel["id"], channel["name"],
                    route["name"] if route else "-")
        return f"created channel {channel['id']}"
    if action == "forward":
        url = str((route.get("config") or {}).get("webhook_url") or "")
        if not url:
            raise RuntimeError("forward route has no webhook URL")
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.post(url, json=payload,
                                         headers={"X-QQ-Appid": app_id, "X-QQ-Event": str(payload.get("t") or "")})
        if response.status_code >= 400:
            raise RuntimeError(f"forward slot returned HTTP {response.status_code}")
        return f"forwarded to {urlparse(url).hostname or url}"
    if action == "party":
        return await qq_party.handle_event(db, credentials, app_id, data,
                                           (route.get("config") or {}) if route else {})
    return ""
