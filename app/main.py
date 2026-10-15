import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import Database, ROUTE_SECRET_FIELDS, SECRET_FIELDS
from .models import ChannelInput, ChannelSummary, MessageInput, RouteInput, SubscriptionInput
from .qq_callback import CallbackRejected, handle_callback, validate_route
from .senders import send, validate_config, valid_email
from .settings import Settings, get_settings
from .subscriptions import PollRateLimited, SubscriptionPoller, validate_subscription


logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent.parent / "static"


def create_app(settings: Settings | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_path, settings.encryption_key)
    poller = SubscriptionPoller(db)

    async def worker():
        while True:
            try:
                item = db.claim_due()
                if not item:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    channel = db.get_channel(item["channel_id"], include_config=True)
                    if not channel or not channel["enabled"]:
                        raise RuntimeError("Channel is unavailable or disabled")
                    config = channel["config"]
                    if channel["kind"] == "qq" and str(config.get("group_openid") or "").strip():
                        # QQ closed bot push messages in 2025-04; deliver by
                        # replying passively to the newest inbound message.
                        openid = str(config["group_openid"]).strip()
                        msg_id = db.latest_qq_interaction_msgid(openid, str(config.get("chat_type") or "group"))
                        if msg_id:
                            seq = db.claim_qq_reply_seq(msg_id)
                            if seq:
                                config = {**config, "msg_id": msg_id, "msg_seq": seq}
                            else:
                                raise RuntimeError("QQ 被动回复次数已用完（同一条消息最多回复 4 次），请先在 QQ 里重新 @ 机器人")
                    await send(channel["kind"], config, item["title"], item["body"], json.loads(item["recipients_json"]))
                except Exception as exc:
                    logger.warning("Delivery %s via channel %s failed (attempt %s/%s): %s", item["id"], item["channel_id"], item["attempts"] + 1, item["max_attempts"], exc)
                    db.finish_delivery(item["id"], item["attempts"] + 1, item["max_attempts"], str(exc))
                else:
                    db.finish_delivery(item["id"], item["attempts"] + 1, item["max_attempts"])
                    logger.info("Delivery %s via channel %s sent", item["id"], item["channel_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Message worker error")
                await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = [asyncio.create_task(worker()), asyncio.create_task(poller.run())] if start_worker else []
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Message Manager", version="0.1.0", lifespan=lifespan)
    app.state.db = db
    app.state.settings = settings
    app.state.subscription_poller = poller

    def require_admin(authorization: str = Header(default="")):
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, settings.admin_token):
            raise HTTPException(401, "Invalid admin token")

    def require_api_key(x_api_key: str = Header(default="")):
        if not hmac.compare_digest(x_api_key, settings.message_api_key):
            raise HTTPException(401, "Invalid API key")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/qq/callback/{app_id}")
    async def qq_callback(app_id: str, request: Request):
        # Called by the QQ open platform; authenticated by the Ed25519 signature
        # the platform derives from the bot's AppSecret, not by our admin token.
        raw_body = await request.body()
        try:
            return await handle_callback(db, app_id, raw_body,
                                         request.headers.get("x-signature-ed25519", ""),
                                         request.headers.get("x-signature-timestamp", ""))
        except CallbackRejected as exc:
            logger.warning("QQ callback for app %s rejected (%s): %s", app_id, exc.status, exc)
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/admin/qq/routes", dependencies=[Depends(require_admin)])
    def qq_routes():
        return db.list_qq_routes()

    def save_route(data: RouteInput, route_id: str | None = None):
        existing = db.get_qq_route(route_id, include_config=True) if route_id else None
        if route_id and not existing:
            raise HTTPException(404, "Route not found")
        config = dict(data.config)
        if existing:
            for key in ROUTE_SECRET_FIELDS:
                if not config.get(key):
                    config[key] = existing["config"].get(key, "")
        try:
            validate_route(data, config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        data.config = config
        return db.save_qq_route(data, route_id)

    @app.post("/api/admin/qq/routes", status_code=201, dependencies=[Depends(require_admin)])
    def create_route(data: RouteInput):
        return save_route(data)

    @app.put("/api/admin/qq/routes/{route_id}", dependencies=[Depends(require_admin)])
    def update_route(route_id: str, data: RouteInput):
        return save_route(data, route_id)

    @app.delete("/api/admin/qq/routes/{route_id}", dependencies=[Depends(require_admin)])
    def delete_route(route_id: str):
        if not db.delete_qq_route(route_id):
            raise HTTPException(404, "Route not found")
        return {"deleted": True}

    @app.get("/api/admin/qq/events", dependencies=[Depends(require_admin)])
    def qq_events(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        return db.list_qq_events(limit, offset)

    @app.delete("/api/admin/qq/events", dependencies=[Depends(require_admin)])
    def clear_qq_events():
        return {"cleared": db.clear_qq_events()}

    @app.get("/api/v1/channels", response_model=list[ChannelSummary], dependencies=[Depends(require_api_key)])
    def public_channels():
        return db.list_public_channels()

    @app.post("/api/v1/messages", status_code=202, dependencies=[Depends(require_api_key)])
    def submit_message(data: MessageInput):
        try:
            message, created = db.create_message(data)
        except ValueError as exc:
            logger.warning("Message submission rejected: %s", exc)
            raise HTTPException(422, str(exc)) from exc
        logger.info("Message %s %s", message["id"], "queued" if created else "already exists")
        return {"id": message["id"], "created": created, "status": "queued", "deliveries": message["deliveries"]}

    @app.get("/api/v1/messages/{message_id}", dependencies=[Depends(require_api_key)])
    def message_status(message_id: str):
        message = db.get_message(message_id)
        if not message:
            raise HTTPException(404, "Message not found")
        return message

    @app.get("/api/admin/session", dependencies=[Depends(require_admin)])
    def admin_session():
        return {"authenticated": True}

    @app.get("/api/admin/stats", dependencies=[Depends(require_admin)])
    def stats():
        return db.stats()

    @app.get("/api/admin/channels", dependencies=[Depends(require_admin)])
    def channels():
        return db.list_channels()

    @app.get("/api/admin/subscriptions", dependencies=[Depends(require_admin)])
    def subscriptions():
        return db.list_subscriptions()

    def save_subscription(data: SubscriptionInput, subscription_id=None):
        existing = db.get_subscription(subscription_id, include_config=True) if subscription_id else None
        if subscription_id and not existing:
            raise HTTPException(404, "Subscription not found")
        config = dict(data.config)
        if existing and existing["kind"] == data.kind:
            for key in ("feed_url", "bearer_token", "password"):
                if not config.get(key):
                    config[key] = existing["config"].get(key, "")
        try:
            data.config = validate_subscription(data.kind, config)
            return db.save_subscription(data, subscription_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/admin/subscriptions", status_code=201, dependencies=[Depends(require_admin)])
    def create_subscription(data: SubscriptionInput):
        return save_subscription(data)

    @app.put("/api/admin/subscriptions/{subscription_id}", dependencies=[Depends(require_admin)])
    def update_subscription(subscription_id: str, data: SubscriptionInput):
        return save_subscription(data, subscription_id)

    @app.delete("/api/admin/subscriptions/{subscription_id}", dependencies=[Depends(require_admin)])
    def delete_subscription(subscription_id: str):
        if not db.delete_subscription(subscription_id):
            raise HTTPException(404, "Subscription not found")
        return {"deleted": True}

    @app.post("/api/admin/subscriptions/{subscription_id}/check", dependencies=[Depends(require_admin)])
    async def check_subscription(subscription_id: str):
        existing = db.get_subscription(subscription_id)
        if not existing:
            raise HTTPException(404, "Subscription not found")
        try:
            return await poller.check(subscription_id)
        except PollRateLimited as exc:
            raise HTTPException(429, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    def save_channel(data: ChannelInput, channel_id: str | None = None):
        existing = db.get_channel(channel_id, include_config=True) if channel_id else None
        if channel_id and not existing:
            raise HTTPException(404, "Channel not found")
        config = dict(data.config)
        if existing and existing["kind"] == data.kind:
            for key in SECRET_FIELDS[data.kind]:
                if not config.get(key):
                    config[key] = existing["config"].get(key, "")
        try:
            validate_config(data.kind, config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        data.config = config
        return db.save_channel(data, channel_id)

    @app.post("/api/admin/channels", status_code=201, dependencies=[Depends(require_admin)])
    def create_channel(data: ChannelInput):
        return save_channel(data)

    @app.put("/api/admin/channels/{channel_id}", dependencies=[Depends(require_admin)])
    def update_channel(channel_id: str, data: ChannelInput):
        return save_channel(data, channel_id)

    @app.delete("/api/admin/channels/{channel_id}", dependencies=[Depends(require_admin)])
    def delete_channel(channel_id: str):
        result = db.delete_channel(channel_id)
        if result == "used":
            raise HTTPException(409, "Channel has delivery history or subscriptions; disable it instead")
        if result == "missing":
            raise HTTPException(404, "Channel not found")
        return {"deleted": True}

    @app.get("/api/admin/messages", dependencies=[Depends(require_admin)])
    def messages(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        return db.list_messages(limit, offset)

    @app.post("/api/admin/messages", status_code=202, dependencies=[Depends(require_admin)])
    def admin_submit(data: MessageInput):
        try:
            message, created = db.create_message(data)
        except ValueError as exc:
            logger.warning("Admin message submission rejected: %s", exc)
            raise HTTPException(422, str(exc)) from exc
        logger.info("Admin message %s %s", message["id"], "queued" if created else "already exists")
        return {"id": message["id"], "created": created, "status": "queued"}

    @app.get("/api/admin/messages/{message_id}", dependencies=[Depends(require_admin)])
    def admin_message_status(message_id: str):
        message = db.get_message(message_id)
        if not message:
            raise HTTPException(404, "Message not found")
        return message

    @app.post("/api/admin/deliveries/{delivery_id}/retry", dependencies=[Depends(require_admin)])
    def retry(delivery_id: str):
        if not db.retry_delivery(delivery_id):
            raise HTTPException(409, "Only failed deliveries can be retried")
        return {"queued": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
