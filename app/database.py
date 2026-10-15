import json
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from .models import ChannelInput, MessageInput, RouteInput, SubscriptionInput
from .senders import valid_email


SECRET_FIELDS = {
    "dingtalk": {"webhook_url", "secret"},
    "feishu": {"webhook_url", "secret"},
    "qq": {"client_secret"},
    "email": {"password"},
}

ROUTE_SECRET_FIELDS = {"webhook_url"}

# The seeded catch-all rule keeps "auto-create a channel" working out of the
# box but evaluates after every user-created rule.
DEFAULT_ROUTE_POSITION = 1000000


class Database:
    def __init__(self, path: str, encryption_key: bytes):
        self.path = path
        self.cipher = Fernet(encryption_key)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def initialize(self):
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS channels (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    enabled INTEGER NOT NULL, config_encrypted BLOB NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                    idempotency_key TEXT, created_at REAL NOT NULL,
                    UNIQUE(source, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
                    channel_id TEXT NOT NULL REFERENCES channels(id), recipients_json TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3, next_attempt_at REAL NOT NULL,
                    last_error TEXT, sent_at REAL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status, next_attempt_at);
                CREATE INDEX IF NOT EXISTS deliveries_message ON deliveries(message_id);
                CREATE INDEX IF NOT EXISTS messages_created ON messages(created_at DESC);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    enabled INTEGER NOT NULL, config_encrypted BLOB NOT NULL,
                    destinations_json TEXT NOT NULL, poll_interval INTEGER NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0, state_json TEXT NOT NULL DEFAULT '{}',
                    next_poll_at REAL NOT NULL, last_checked_at REAL, last_success_at REAL, last_error TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscription_entries (
                    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    entry_id TEXT NOT NULL, PRIMARY KEY(subscription_id, entry_id)
                );
                CREATE INDEX IF NOT EXISTS subscriptions_due ON subscriptions(enabled, next_poll_at);
                CREATE TABLE IF NOT EXISTS qq_routes (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL,
                    position INTEGER NOT NULL, event_types TEXT NOT NULL,
                    chat_type TEXT NOT NULL DEFAULT 'any', openid TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL, config_encrypted BLOB NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qq_events (
                    id TEXT PRIMARY KEY, received_at REAL NOT NULL, app_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, chat_type TEXT, openid TEXT, payload_json TEXT NOT NULL,
                    route_id TEXT, action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', detail TEXT
                );
                CREATE INDEX IF NOT EXISTS qq_events_received ON qq_events(received_at DESC);
                CREATE TABLE IF NOT EXISTS qq_reply_seq (
                    msg_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qq_parties (
                    id TEXT PRIMARY KEY, group_openid TEXT NOT NULL, day TEXT NOT NULL,
                    creator_openid TEXT NOT NULL, creator_name TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, UNIQUE(group_openid, day)
                );
                CREATE TABLE IF NOT EXISTS qq_party_members (
                    party_id TEXT NOT NULL REFERENCES qq_parties(id),
                    member_openid TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                    expected_time TEXT NOT NULL DEFAULT '', joined_at REAL NOT NULL,
                    UNIQUE(party_id, member_openid)
                );
            """)
            subscription_columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscriptions)")}
            if "last_success_at" not in subscription_columns:
                conn.execute("ALTER TABLE subscriptions ADD COLUMN last_success_at REAL")
                # Existing subscriptions start the new policy at the check time
                # already shown to the user; subsequent failures preserve it.
                conn.execute("UPDATE subscriptions SET last_success_at=last_checked_at WHERE initialized=1")
            if conn.execute("SELECT COUNT(*) FROM qq_routes").fetchone()[0] == 0:
                conn.execute("INSERT INTO qq_routes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (str(uuid.uuid4()), "兜底：自动创建渠道", 1, DEFAULT_ROUTE_POSITION,
                              json.dumps(["*"]), "any", "", "channel",
                              self.cipher.encrypt(b"{}"), time.time(), time.time()))
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
            if "source" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'default'")
            legacy_unique = any(
                row["unique"] and [column["name"] for column in conn.execute("SELECT name FROM pragma_index_info(?)", (row["name"],))] == ["idempotency_key"]
                for row in conn.execute("PRAGMA index_list(messages)")
            )
            if legacy_unique:
                self._migrate_legacy_message_unique(conn)
            conn.execute("UPDATE deliveries SET status='pending' WHERE status='sending'")

    def _migrate_legacy_message_unique(self, conn):
        # The original table enforced a global idempotency key. Rebuild it so keys
        # are scoped to source, keeping existing messages and delivery references.
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE messages_migrated (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
                body TEXT NOT NULL, idempotency_key TEXT, created_at REAL NOT NULL,
                UNIQUE(source, idempotency_key)
            )""")
            conn.execute("""INSERT INTO messages_migrated (id, source, title, body, idempotency_key, created_at)
                SELECT id, source, title, body, idempotency_key, created_at FROM messages""")
            conn.execute("DROP TABLE messages")
            conn.execute("ALTER TABLE messages_migrated RENAME TO messages")
            conn.execute("CREATE INDEX messages_created ON messages(created_at DESC)")
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Legacy message migration would break delivery references")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def _decode(self, row):
        return json.loads(self.cipher.decrypt(row["config_encrypted"]))

    def _subscription(self, row, include_config=False):
        result = {key: row[key] for key in ("id", "name", "kind", "poll_interval", "next_poll_at",
                  "last_checked_at", "last_success_at", "last_error", "created_at", "updated_at")}
        result["enabled"] = bool(row["enabled"])
        result["initialized"] = bool(row["initialized"])
        result["destinations"] = json.loads(row["destinations_json"])
        config = self._decode(row)
        secrets = {"feed_url", "bearer_token", "password"}
        if include_config:
            result["config"] = config
            result["state"] = json.loads(row["state_json"])
        else:
            result["config"] = {key: "" if key in secrets else value for key, value in config.items()}
            result["configured_secrets"] = [key for key in secrets if config.get(key)]
            result["feed_host"] = urlparse(str(config.get("feed_url") or "")).hostname
        return result

    def list_subscriptions(self, due_only=False):
        with self.connect() as conn:
            if due_only:
                rows = conn.execute("SELECT * FROM subscriptions WHERE enabled=1 AND next_poll_at<=? ORDER BY next_poll_at", (time.time(),))
            else:
                rows = conn.execute("SELECT * FROM subscriptions ORDER BY created_at DESC")
            return [self._subscription(row) for row in rows]

    def get_subscription(self, subscription_id, include_config=False):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
            return self._subscription(row, include_config) if row else None

    def save_subscription(self, data: SubscriptionInput, subscription_id=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone() if subscription_id else None
            if subscription_id and not old:
                return None
            self.validate_destinations(data.destinations, conn)
            now = time.time()
            config = self.cipher.encrypt(json.dumps(data.config).encode())
            destinations = json.dumps([item.model_dump() for item in data.destinations])
            if old:
                previous = self._decode(old)
                # A changed source/filter starts a new baseline. Credential and routing
                # changes preserve the cursor and all observed entries.
                reset = (old["kind"] != data.kind or
                         any(previous.get(key) != data.config.get(key) for key in
                             ("feed_url", "username", "include_replies", "include_reposts", "provider")))
                conn.execute("""UPDATE subscriptions SET name=?,kind=?,enabled=?,config_encrypted=?,
                    destinations_json=?,poll_interval=?,next_poll_at=?,last_error=NULL,updated_at=? WHERE id=?""",
                    (data.name, data.kind, int(data.enabled), config, destinations, data.poll_interval, now, now, subscription_id))
                if reset:
                    conn.execute("UPDATE subscriptions SET initialized=0,state_json='{}',last_checked_at=NULL,last_success_at=NULL WHERE id=?", (subscription_id,))
                    conn.execute("DELETE FROM subscription_entries WHERE subscription_id=?", (subscription_id,))
            else:
                subscription_id = str(uuid.uuid4())
                conn.execute("""INSERT INTO subscriptions
                    (id,name,kind,enabled,config_encrypted,destinations_json,poll_interval,next_poll_at,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (subscription_id, data.name, data.kind, int(data.enabled), config, destinations, data.poll_interval, now, now, now))
        return self.get_subscription(subscription_id)

    def delete_subscription(self, subscription_id):
        with self.connect() as conn:
            return conn.execute("DELETE FROM subscriptions WHERE id=?", (subscription_id,)).rowcount > 0

    def apply_subscription_entries(self, subscription, entries, state):
        """Queue new messages and save observed IDs/cursor in the same transaction."""
        from .models import Destination

        count = 0
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription["id"],)).fetchone()
            if not row or row["updated_at"] != subscription["updated_at"] or not row["enabled"]:
                return 0
            window_end = subscription.get("window_end", now)
            window_start = subscription.get("window_start", row["last_success_at"])
            baseline = window_start is None
            for entry in entries:
                published_at = entry.get("published_at")
                # Future entries must remain eligible for the next check.
                # Undated entries cannot satisfy a strict time window.
                if published_at is None or published_at > window_end:
                    continue
                observed = conn.execute("INSERT OR IGNORE INTO subscription_entries VALUES (?,?)", (row["id"], entry["id"]))
                if not observed.rowcount or baseline or published_at <= window_start:
                    continue
                data = MessageInput(source=f"subscription:{row['id']}", title=entry["title"][:200],
                                    body=entry["body"][:20000], idempotency_key=entry["id"],
                                    destinations=[Destination(**item) for item in subscription["destinations"]])
                _, created = self._insert_message(conn, data, now)
                count += int(created)
            conn.execute("""UPDATE subscriptions SET initialized=1,state_json=?,last_checked_at=?,last_success_at=?,last_error=NULL,
                next_poll_at=? WHERE id=?""", (json.dumps(state), window_end, window_end, now + row["poll_interval"], row["id"]))
        return count

    def fail_subscription(self, subscription, error, retry_after=0):
        now = time.time()
        with self.connect() as conn:
            conn.execute("""UPDATE subscriptions SET last_checked_at=?,last_error=?,next_poll_at=?
                WHERE id=? AND updated_at=?""",
                (subscription.get("window_end", now), str(error)[:500], now + max(subscription["poll_interval"], retry_after),
                 subscription["id"], subscription["updated_at"]))

    def _channel(self, row, include_config=False):
        result = {key: row[key] for key in ("id", "name", "kind", "created_at", "updated_at")}
        result["enabled"] = bool(row["enabled"])
        config = self._decode(row)
        if include_config:
            result["config"] = config
        else:
            result["config"] = {key: ("" if key in SECRET_FIELDS[row["kind"]] else value) for key, value in config.items()}
            result["configured_secrets"] = [key for key in SECRET_FIELDS[row["kind"]] if config.get(key)]
            if row["kind"] in ("dingtalk", "feishu"):
                result["webhook_host"] = urlparse(str(config.get("webhook_url") or "")).hostname
        return result

    def list_channels(self):
        with self.connect() as conn:
            return [self._channel(row) for row in conn.execute("SELECT * FROM channels ORDER BY created_at DESC")]

    def get_channel(self, channel_id, include_config=False):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
            return self._channel(row, include_config) if row else None

    def find_qq_channel(self, openid: str, chat_type: str = "group"):
        with self.connect() as conn:
            for row in conn.execute("SELECT * FROM channels WHERE kind='qq'"):
                config = self._decode(row)
                if str(config.get("group_openid") or "").strip() == openid and str(config.get("chat_type") or "group") == chat_type:
                    return self._channel(row)
        return None

    def qq_credentials(self, app_id: str) -> dict:
        """Credentials of any stored QQ channel for this app, so the callback
        can verify signatures and stamp new channels without an .env entry."""
        with self.connect() as conn:
            for row in conn.execute("SELECT * FROM channels WHERE kind='qq'"):
                config = self._decode(row)
                if str(config.get("app_id") or "").strip() == app_id and str(config.get("client_secret") or "").strip():
                    return {"app_id": app_id, "client_secret": str(config["client_secret"])}
        return {"app_id": app_id, "client_secret": ""}

    def _route(self, row, include_config=False):
        result = {key: row[key] for key in ("id", "name", "position", "chat_type", "openid", "action")}
        result["enabled"] = bool(row["enabled"])
        result["event_types"] = json.loads(row["event_types"])
        config = json.loads(self.cipher.decrypt(row["config_encrypted"]))
        if include_config:
            result["config"] = config
        else:
            result["config"] = {key: ("" if key in ROUTE_SECRET_FIELDS else value) for key, value in config.items()}
            result["configured_secrets"] = [key for key in ROUTE_SECRET_FIELDS if config.get(key)]
        return result

    def list_qq_routes(self):
        with self.connect() as conn:
            return [self._route(row) for row in conn.execute("SELECT * FROM qq_routes ORDER BY position, created_at")]

    def get_qq_route(self, route_id, include_config=False):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM qq_routes WHERE id=?", (route_id,)).fetchone()
            return self._route(row, include_config) if row else None

    def save_qq_route(self, data: RouteInput, route_id=None):
        now = time.time()
        with self.connect() as conn:
            old = conn.execute("SELECT * FROM qq_routes WHERE id=?", (route_id,)).fetchone() if route_id else None
            if route_id and not old:
                return None
            config = dict(data.config)
            if old:
                previous = json.loads(self.cipher.decrypt(old["config_encrypted"]))
                for key in ROUTE_SECRET_FIELDS:
                    if not config.get(key):
                        config[key] = previous.get(key, "")
            encrypted = self.cipher.encrypt(json.dumps(config).encode())
            if old:
                conn.execute("""UPDATE qq_routes SET name=?,enabled=?,event_types=?,chat_type=?,openid=?,action=?,
                    config_encrypted=?,updated_at=? WHERE id=?""",
                             (data.name, int(data.enabled), json.dumps(data.event_types), data.chat_type,
                              data.openid.strip(), data.action, encrypted, now, route_id))
            else:
                position = conn.execute("SELECT MIN(1000000 - 1, COALESCE(MAX(position), 0) + 1) FROM qq_routes").fetchone()[0]
                route_id = str(uuid.uuid4())
                conn.execute("INSERT INTO qq_routes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (route_id, data.name, int(data.enabled), position, json.dumps(data.event_types),
                              data.chat_type, data.openid.strip(), data.action, encrypted, now, now))
        return self.get_qq_route(route_id)

    def delete_qq_route(self, route_id):
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM qq_routes WHERE id=?", (route_id,))
            return cursor.rowcount > 0

    def record_qq_event(self, app_id, event_type, chat_type, openid, payload, route_id, action):
        event_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute("INSERT INTO qq_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (event_id, time.time(), app_id, event_type, chat_type, openid,
                          json.dumps(payload, ensure_ascii=False), route_id, action, "pending", None))
        return event_id

    def finish_qq_event(self, event_id, status, detail=None):
        with self.connect() as conn:
            conn.execute("UPDATE qq_events SET status=?,detail=? WHERE id=?", (status, str(detail)[:500] if detail else None, event_id))

    def list_qq_events(self, limit=50, offset=0):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM qq_events ORDER BY received_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM qq_events").fetchone()[0]
        items = [{
            "id": row["id"], "received_at": row["received_at"], "app_id": row["app_id"],
            "event_type": row["event_type"], "chat_type": row["chat_type"], "openid": row["openid"],
            "payload": json.loads(row["payload_json"]), "route_id": row["route_id"],
            "action": row["action"], "status": row["status"], "detail": row["detail"],
        } for row in rows]
        return {"items": items, "total": total}

    def clear_qq_events(self):
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM qq_events")
            return cursor.rowcount

    def latest_qq_interaction_msgid(self, openid: str, chat_type: str, max_age: float = 3600):
        """Message id of the newest inbound group/@ or private message for a
        target, within the passive-reply window; None when the window is gone."""
        with self.connect() as conn:
            row = conn.execute("""SELECT payload_json FROM qq_events
                WHERE event_type IN ('GROUP_AT_MESSAGE_CREATE','C2C_MESSAGE_CREATE')
                  AND chat_type=? AND openid=? AND received_at>=?
                ORDER BY received_at DESC LIMIT 1""",
                (chat_type, openid, time.time() - max_age)).fetchone()
        if not row:
            return None
        message_id = (json.loads(row["payload_json"]).get("d") or {}).get("id")
        return str(message_id) if message_id else None

    def claim_qq_reply_seq(self, msg_id: str, limit: int = 4):
        """Reserve the next passive-reply slot for an inbound message.
        Returns 1..limit, or None once the message is fully replied to."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT seq FROM qq_reply_seq WHERE msg_id=?", (msg_id,)).fetchone()
            if row:
                if row["seq"] >= limit:
                    return None
                seq = row["seq"] + 1
                conn.execute("UPDATE qq_reply_seq SET seq=?,updated_at=? WHERE msg_id=?",
                             (seq, time.time(), msg_id))
            else:
                seq = 1
                conn.execute("INSERT INTO qq_reply_seq VALUES (?,?,?)", (msg_id, seq, time.time()))
            return seq

    def qq_party_today(self, group_openid: str):
        day = time.strftime("%Y-%m-%d", time.localtime())
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM qq_parties WHERE group_openid=? AND day=?",
                               (group_openid, day)).fetchone()
            return dict(row) if row else None

    def qq_party_ensure(self, group_openid: str, creator_openid: str, creator_name: str = "") -> dict:
        day = time.strftime("%Y-%m-%d", time.localtime())
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM qq_parties WHERE group_openid=? AND day=?",
                               (group_openid, day)).fetchone()
            if not row:
                conn.execute("INSERT INTO qq_parties VALUES (?,?,?,?,?,?)",
                             (str(uuid.uuid4()), group_openid, day, creator_openid,
                              creator_name, time.time()))
                row = conn.execute("SELECT * FROM qq_parties WHERE group_openid=? AND day=?",
                                   (group_openid, day)).fetchone()
            elif creator_name and not row["creator_name"] and row["creator_openid"] == creator_openid:
                conn.execute("UPDATE qq_parties SET creator_name=? WHERE id=?", (creator_name, row["id"]))
                row = conn.execute("SELECT * FROM qq_parties WHERE id=?", (row["id"],)).fetchone()
        return dict(row)

    def qq_party_join(self, party_id: str, member_openid: str, name: str, expected_time: str):
        with self.connect() as conn:
            existing = conn.execute("SELECT name, expected_time FROM qq_party_members WHERE party_id=? AND member_openid=?",
                                    (party_id, member_openid)).fetchone()
            if existing:
                conn.execute("UPDATE qq_party_members SET name=?,expected_time=? WHERE party_id=? AND member_openid=?",
                             (name or existing["name"], expected_time or existing["expected_time"],
                              party_id, member_openid))
            else:
                conn.execute("INSERT INTO qq_party_members VALUES (?,?,?,?,?)",
                             (party_id, member_openid, name, expected_time, time.time()))

    def qq_party_leave(self, party_id: str, member_openid: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM qq_party_members WHERE party_id=? AND member_openid=?",
                                  (party_id, member_openid))
            return cursor.rowcount > 0

    def qq_party_members(self, party_id: str) -> list[dict]:
        with self.connect() as conn:
            return [{"member_openid": row["member_openid"], "name": row["name"],
                     "expected_time": row["expected_time"]}
                    for row in conn.execute("SELECT * FROM qq_party_members WHERE party_id=? ORDER BY joined_at",
                                            (party_id,))]

    def save_channel(self, data: ChannelInput, channel_id=None):
        now = time.time()
        with self.connect() as conn:
            old = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone() if channel_id else None
            if channel_id and not old:
                return None
            config = dict(data.config)
            if old and old["kind"] == data.kind:
                previous = self._decode(old)
                for key in SECRET_FIELDS[data.kind]:
                    if not config.get(key):
                        config[key] = previous.get(key, "")
            encrypted = self.cipher.encrypt(json.dumps(config).encode())
            if old:
                conn.execute("UPDATE channels SET name=?,kind=?,enabled=?,config_encrypted=?,updated_at=? WHERE id=?",
                             (data.name, data.kind, int(data.enabled), encrypted, now, channel_id))
            else:
                channel_id = str(uuid.uuid4())
                conn.execute("INSERT INTO channels VALUES (?,?,?,?,?,?,?)",
                             (channel_id, data.name, data.kind, int(data.enabled), encrypted, now, now))
        return self.get_channel(channel_id)

    def delete_channel(self, channel_id):
        with self.connect() as conn:
            used = conn.execute("SELECT 1 FROM deliveries WHERE channel_id=? LIMIT 1", (channel_id,)).fetchone()
            if used:
                return "used"
            for row in conn.execute("SELECT destinations_json FROM subscriptions"):
                if any(item["channel_id"] == channel_id for item in json.loads(row["destinations_json"])):
                    return "used"
            cursor = conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
            return "deleted" if cursor.rowcount else "missing"

    def validate_destinations(self, destinations, conn):
        ids = [destination.channel_id for destination in destinations]
        rows = conn.execute(f"SELECT id,kind,enabled,config_encrypted FROM channels WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()
        channels = {row["id"]: row for row in rows}
        for destination in destinations:
            row = channels.get(destination.channel_id)
            if not row or not row["enabled"]:
                raise ValueError(f"Channel {destination.channel_id} does not exist or is disabled")
            if row["kind"] == "email" and not (destination.recipients or self._decode(row).get("default_recipients")):
                raise ValueError(f"Email channel {destination.channel_id} has no recipients")
            if row["kind"] == "email" and any(not valid_email(item) for item in destination.recipients):
                raise ValueError("Invalid email recipient address")
            if row["kind"] != "email" and destination.recipients:
                raise ValueError("Recipients are only supported for email channels")
            if row["kind"] == "qq" and not str(self._decode(row).get("group_openid") or "").strip():
                raise ValueError(f"QQ channel {destination.channel_id} has no target OpenID; it only identifies the bot")

    def _insert_message(self, conn, data, now):
        if data.idempotency_key:
            prior = conn.execute("SELECT id FROM messages WHERE source=? AND idempotency_key=?",
                                 (data.source, data.idempotency_key)).fetchone()
            if prior:
                return prior["id"], False
        self.validate_destinations(data.destinations, conn)
        message_id = str(uuid.uuid4())
        conn.execute("""INSERT INTO messages (id, source, title, body, idempotency_key, created_at)
            VALUES (?,?,?,?,?,?)""", (message_id, data.source, data.title, data.body, data.idempotency_key, now))
        for destination in data.destinations:
            conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (str(uuid.uuid4()), message_id, destination.channel_id, json.dumps(destination.recipients), "pending", 0, 3, now, None, None, now))
        return message_id, True

    def create_message(self, data: MessageInput):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            message_id, created = self._insert_message(conn, data, time.time())
        return self.get_message(message_id), created

    def get_message(self, message_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                return None
            deliveries = conn.execute("""SELECT d.*, c.name AS channel_name, c.kind AS channel_kind
                FROM deliveries d JOIN channels c ON c.id=d.channel_id WHERE d.message_id=? ORDER BY d.created_at""", (message_id,)).fetchall()
            return {"id": row["id"], "source": row["source"], "title": row["title"], "body": row["body"],
                    "idempotency_key": row["idempotency_key"], "created_at": row["created_at"],
                    "deliveries": [self._delivery(item) for item in deliveries]}

    def _delivery(self, row):
        return {"id": row["id"], "channel_id": row["channel_id"], "channel_name": row["channel_name"],
                "channel_kind": row["channel_kind"], "recipients": json.loads(row["recipients_json"]),
                "status": row["status"], "attempts": row["attempts"], "max_attempts": row["max_attempts"],
                "next_attempt_at": row["next_attempt_at"], "last_error": row["last_error"], "sent_at": row["sent_at"]}

    def list_messages(self, limit=50, offset=0):
        with self.connect() as conn:
            ids = [row["id"] for row in conn.execute("SELECT id FROM messages ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))]
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return {"items": [self.get_message(item) for item in ids], "total": total}

    def stats(self):
        with self.connect() as conn:
            return {
                "channels": conn.execute("SELECT COUNT(*) FROM channels WHERE enabled=1").fetchone()[0],
                "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "sent": conn.execute("SELECT COUNT(*) FROM deliveries WHERE status='sent'").fetchone()[0],
                "pending": conn.execute("SELECT COUNT(*) FROM deliveries WHERE status IN ('pending','sending')").fetchone()[0],
                "failed": conn.execute("SELECT COUNT(*) FROM deliveries WHERE status='failed'").fetchone()[0],
            }

    def claim_due(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT d.id, d.message_id, d.channel_id, d.recipients_json, d.attempts,
                d.max_attempts, m.title, m.body FROM deliveries d JOIN messages m ON m.id=d.message_id
                WHERE d.status='pending' AND d.next_attempt_at<=? ORDER BY d.next_attempt_at LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE deliveries SET status='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
            return dict(row)

    def finish_delivery(self, delivery_id, attempts, max_attempts, error=None):
        with self.connect() as conn:
            if error is None:
                conn.execute("UPDATE deliveries SET status='sent',last_error=NULL,sent_at=? WHERE id=?", (time.time(), delivery_id))
            else:
                status = "failed" if attempts >= max_attempts else "pending"
                delay = min(300, 2 ** attempts)
                conn.execute("UPDATE deliveries SET status=?,last_error=?,next_attempt_at=? WHERE id=?",
                             (status, str(error)[:500], time.time() + delay, delivery_id))

    def retry_delivery(self, delivery_id):
        with self.connect() as conn:
            cursor = conn.execute("""UPDATE deliveries SET status='pending',attempts=0,last_error=NULL,next_attempt_at=?
                WHERE id=? AND status='failed'""", (time.time(), delivery_id))
            return cursor.rowcount > 0
