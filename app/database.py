import json
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from .models import ChannelInput, MessageInput
from .senders import valid_email


SECRET_FIELDS = {"dingtalk": {"webhook_url", "secret"}, "feishu": {"webhook_url", "secret"}, "email": {"password"}}


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
            """)
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
            cursor = conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
            return "deleted" if cursor.rowcount else "missing"

    def create_message(self, data: MessageInput):
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if data.idempotency_key:
                prior = conn.execute("SELECT id FROM messages WHERE source=? AND idempotency_key=?", (data.source, data.idempotency_key)).fetchone()
                if prior:
                    return self.get_message(prior["id"]), False
            ids = [destination.channel_id for destination in data.destinations]
            rows = conn.execute(f"SELECT id,kind,enabled,config_encrypted FROM channels WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()
            channels = {row["id"]: row for row in rows}
            for destination in data.destinations:
                row = channels.get(destination.channel_id)
                if not row or not row["enabled"]:
                    raise ValueError(f"Channel {destination.channel_id} does not exist or is disabled")
                if row["kind"] == "email" and not (destination.recipients or self._decode(row).get("default_recipients")):
                    raise ValueError(f"Email channel {destination.channel_id} has no recipients")
                if row["kind"] == "email" and any(not valid_email(item) for item in destination.recipients):
                    raise ValueError("Invalid email recipient address")
                if row["kind"] != "email" and destination.recipients:
                    raise ValueError("Recipients are only supported for email channels")
            message_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO messages (id, source, title, body, idempotency_key, created_at)
                VALUES (?,?,?,?,?,?)""", (message_id, data.source, data.title, data.body, data.idempotency_key, now))
            for destination in data.destinations:
                conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (str(uuid.uuid4()), message_id, destination.channel_id, json.dumps(destination.recipients), "pending", 0, 3, now, None, None, now))
        return self.get_message(message_id), True

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
