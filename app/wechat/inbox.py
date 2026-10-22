"""Durable input/cursor and send journal, in the isolated WeChat runtime database."""
from __future__ import annotations

import hashlib
import json
import time

from ..storage import MemoryStore
from .__main__ import allowed_message
from .client import ProtocolError


class InboxFull(ProtocolError):
    pass


class Inbox(MemoryStore):
    def __init__(self, path, account, *, cursor="", since_ms=None):
        super().__init__(str(path))
        self.account = account
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS wx_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wx_inbox (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wx_outbox (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL, status TEXT NOT NULL,
                response TEXT NOT NULL DEFAULT '{}'
            );
        """)
        fingerprint = hashlib.sha256((account['bot_id'] + ':' + account['owner']).encode()).hexdigest()
        with self._db:
            for key, value in (("account", fingerprint), ("cursor", cursor),
                               ("since_ms", str(since_ms if since_ms is not None else int(time.time()*1000)))):
                self._db.execute("INSERT OR IGNORE INTO wx_meta VALUES (?, ?)", (key, value))
        if self.meta("account") != fingerprint:
            self.close()
            raise ProtocolError("运行库与绑定帐号不匹配；未开始收件。")

    def meta(self, key):
        return self._db.execute("SELECT value FROM wx_meta WHERE key=?", (key,)).fetchone()[0]

    def accept(self, batch, *, limit=100):
        messages = batch.get("msgs", [])
        cursor = batch.get("get_updates_buf", self.meta("cursor"))
        if not isinstance(messages, list) or not isinstance(cursor, str):
            raise ProtocolError("收件批次格式无效，未推进游标。")
        now = int(time.time())
        count = 0
        # A full batch and its cursor commit together, or neither does.
        with self._db:
            for message in messages:
                if not allowed_message(message, self.account):
                    continue
                identity, context = message.get("message_id"), message.get("context_token")
                stamp = message.get("create_time_ms")
                if (isinstance(identity, bool) or not isinstance(identity, (str, int))
                        or not str(identity) or len(str(identity)) > 256
                        or not isinstance(context, str) or not context or len(context) > 16000
                        or not isinstance(stamp, int) or isinstance(stamp, bool)
                        or stamp < int(self.meta("since_ms"))):
                    continue
                payload = json.dumps(message, ensure_ascii=False)
                if len(payload.encode()) > 128 * 1024:
                    # Preserve the reply context and explicitly reject oversized input.
                    payload = json.dumps({**{k: message[k] for k in (
                        "message_id", "context_token", "create_time_ms", "from_user_id", "to_user_id",
                        "message_type", "message_state")}, "item_list": [], "input_rejected": True})
                inserted = self._db.execute(
                    "INSERT OR IGNORE INTO wx_inbox VALUES (?, ?, 'queued', ?, ?)",
                    (str(identity), payload, now, now),
                ).rowcount
                count += inserted
            pending = self._db.execute(
                "SELECT count(*) FROM wx_inbox WHERE status IN ('queued','running','interrupted')"
            ).fetchone()[0]
            if pending > limit:
                raise InboxFull("微信待处理消息已满；保留旧游标，等待队列消化。")
            self._db.execute("UPDATE wx_meta SET value=? WHERE key='cursor'", (cursor,))
        return count

    def recover(self):
        with self._db:
            self._db.execute("UPDATE wx_outbox SET status='unknown' WHERE status='sending'")
            # Never repeat a model/tool execution whose side effects may have happened.
            self._db.execute("UPDATE wx_inbox SET status='interrupted' WHERE status='running'")

    def next(self):
        row = self._db.execute(
            "SELECT id, payload, status FROM wx_inbox WHERE status IN ('queued','interrupted') "
            "ORDER BY rowid LIMIT 1"
        ).fetchone()
        return (row[0], json.loads(row[1]), row[2]) if row else None

    def mark(self, identity, status):
        with self._db:
            self._db.execute("UPDATE wx_inbox SET status=?, updated_at=? WHERE id=?",
                             (status, int(time.time()), identity))
            if status in {"done", "failed", "unknown"}:
                # Do not retain context tokens or CDN keys after completion.
                self._db.execute("UPDATE wx_inbox SET payload='{}' WHERE id=?", (identity,))

    def begin_send(self, identity, request_id):
        row = self._db.execute("SELECT status, response FROM wx_outbox WHERE id=?", (identity,)).fetchone()
        if row:
            return row[0], json.loads(row[1])
        with self._db:
            self._db.execute("INSERT INTO wx_outbox(id,request_id,status) VALUES (?,?,'sending')",
                             (identity, request_id))
        return "new", {}

    def end_send(self, identity, status, response=None):
        with self._db:
            self._db.execute("UPDATE wx_outbox SET status=?,response=? WHERE id=?",
                             (status, json.dumps(response or {}), identity))

    def prune(self, days=30):
        cutoff = int(time.time()) - days * 86400
        with self._db:
            self._db.execute("DELETE FROM wx_outbox WHERE request_id IN (SELECT id FROM wx_inbox "
                             "WHERE updated_at < ? AND status IN ('done','failed','unknown'))", (cutoff,))
            self._db.execute("DELETE FROM wx_inbox WHERE updated_at < ? "
                             "AND status IN ('done','failed','unknown')", (cutoff,))
            # Dropped dedup tombstones cannot make old messages eligible again.
            self._db.execute("UPDATE wx_meta SET value=? WHERE key='since_ms'",
                             (str(max(int(self.meta('since_ms')), cutoff*1000)),))
