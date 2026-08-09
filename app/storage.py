from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .domain import ChatLine


@dataclass(frozen=True, slots=True)
class StorageCleanupStats:
    processed_messages_removed: int = 0
    chat_messages_removed: int = 0
    attachment_records_removed: int = 0


@dataclass(frozen=True, slots=True)
class BackupStats:
    created: bool = False
    bytes_written: int = 0
    backups_removed: int = 0


class MemoryStore:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                processed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                content TEXT NOT NULL,
                is_bot INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_group_id
                ON chat_messages(group_id, id DESC);
            CREATE TABLE IF NOT EXISTS chat_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_message_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_attachment_group_message
                ON chat_attachments(group_id, chat_message_id DESC, id DESC);
            CREATE TABLE IF NOT EXISTS group_settings (
                group_id TEXT NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT NOT NULL,
                PRIMARY KEY(group_id, setting_key)
            );
            """
        )
        self._db.commit()
        self._lock = asyncio.Lock()

    async def claim(self, message_id: str) -> bool:
        async with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO processed_messages(message_id, processed_at) VALUES (?, ?)",
                    (message_id, int(time.time())),
                )
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    async def add(
        self,
        group_id: str,
        user_id: str,
        username: str,
        content: str,
        *,
        is_bot: bool = False,
        attachments: Iterable[tuple[str, str]] = (),
    ) -> int:
        async with self._lock:
            now = int(time.time())
            cursor = self._db.execute(
                """
                INSERT INTO chat_messages(group_id, user_id, username, content, is_bot, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, user_id, username, content, int(is_bot), now),
            )
            chat_message_id = int(cursor.lastrowid)
            self._db.executemany(
                """
                INSERT INTO chat_attachments(
                    chat_message_id, group_id, relative_path, content_type, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (chat_message_id, group_id, relative_path, content_type, now)
                    for relative_path, content_type in attachments
                ),
            )
            self._db.commit()
            return chat_message_id

    async def history(self, group_id: str, limit: int) -> list[ChatLine]:
        async with self._lock:
            rows = self._db.execute(
                """
                SELECT user_id, username, content, is_bot
                FROM chat_messages
                WHERE group_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, limit),
            ).fetchall()
        return [
            ChatLine(row[1], row[2], bool(row[3]), str(row[0]))
            for row in reversed(rows)
        ]

    async def reset_group(self, group_id: str) -> None:
        async with self._lock:
            self._db.execute("DELETE FROM chat_attachments WHERE group_id = ?", (group_id,))
            self._db.execute("DELETE FROM chat_messages WHERE group_id = ?", (group_id,))
            self._db.commit()

    async def recent_image_attachments(
        self, group_id: str, message_limit: int, image_limit: int
    ) -> list[tuple[str, str]]:
        async with self._lock:
            rows = self._db.execute(
                """
                SELECT attachment.relative_path, attachment.content_type
                FROM chat_attachments AS attachment
                JOIN chat_messages AS message ON message.id = attachment.chat_message_id
                WHERE message.group_id = ?
                  AND message.id IN (
                      SELECT id FROM chat_messages
                      WHERE group_id = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
                  AND (
                      lower(attachment.content_type) = 'image'
                      OR lower(attachment.content_type) LIKE 'image/%'
                      OR lower(attachment.relative_path) LIKE '%.jpg'
                      OR lower(attachment.relative_path) LIKE '%.jpeg'
                      OR lower(attachment.relative_path) LIKE '%.png'
                      OR lower(attachment.relative_path) LIKE '%.gif'
                      OR lower(attachment.relative_path) LIKE '%.webp'
                  )
                ORDER BY message.id DESC, attachment.id DESC
                LIMIT ?
                """,
                (group_id, group_id, message_limit, image_limit),
            ).fetchall()
        return [(str(row[0]), str(row[1])) for row in reversed(rows)]

    async def get_setting(self, group_id: str, key: str) -> str | None:
        async with self._lock:
            row = self._db.execute(
                "SELECT setting_value FROM group_settings WHERE group_id = ? AND setting_key = ?",
                (group_id, key),
            ).fetchone()
        return str(row[0]) if row else None

    async def set_setting(self, group_id: str, key: str, value: str) -> None:
        async with self._lock:
            self._db.execute(
                """
                INSERT INTO group_settings(group_id, setting_key, setting_value)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id, setting_key)
                DO UPDATE SET setting_value = excluded.setting_value
                """,
                (group_id, key, value),
            )
            self._db.commit()

    async def cleanup(
        self,
        processed_retention_days: int = 7,
        chat_retention_days: int = 30,
        max_messages_per_group: int = 1000,
    ) -> StorageCleanupStats:
        now = int(time.time())
        processed_cutoff = now - processed_retention_days * 24 * 3600
        chat_cutoff = now - chat_retention_days * 24 * 3600
        async with self._lock:
            processed_cursor = self._db.execute(
                "DELETE FROM processed_messages WHERE processed_at < ?",
                (processed_cutoff,),
            )
            chat_cursor = self._db.execute(
                """
                DELETE FROM chat_messages
                WHERE created_at < ?
                   OR id IN (
                       SELECT id FROM (
                           SELECT id,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY group_id ORDER BY id DESC
                                  ) AS row_number
                           FROM chat_messages
                       )
                       WHERE row_number > ?
                   )
                """,
                (chat_cutoff, max_messages_per_group),
            )
            attachment_cursor = self._db.execute(
                """
                DELETE FROM chat_attachments
                WHERE chat_message_id NOT IN (SELECT id FROM chat_messages)
                """
            )
            self._db.commit()
            self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return StorageCleanupStats(
            processed_messages_removed=max(0, processed_cursor.rowcount),
            chat_messages_removed=max(0, chat_cursor.rowcount),
            attachment_records_removed=max(0, attachment_cursor.rowcount),
        )

    async def backup_if_due(
        self,
        backup_dir: str,
        interval_hours: int,
        retention_days: int,
        *,
        now: float | None = None,
    ) -> BackupStats:
        current = time.time() if now is None else now
        directory = Path(backup_dir)
        directory.mkdir(parents=True, exist_ok=True)
        retention_cutoff = current - retention_days * 24 * 3600
        interval_cutoff = current - interval_hours * 3600
        removed = 0

        async with self._lock:
            for partial in directory.glob(".qqchat-backup-*.db.part"):
                if partial.is_file() and not partial.is_symlink():
                    partial.unlink()
            backups = sorted(
                (
                    path
                    for path in directory.glob("qqchat-backup-*.db")
                    if path.is_file() and not path.is_symlink()
                ),
                key=lambda path: path.stat().st_mtime,
            )
            for path in list(backups):
                if path.stat().st_mtime >= retention_cutoff:
                    continue
                path.unlink()
                backups.remove(path)
                removed += 1

            if backups and backups[-1].stat().st_mtime > interval_cutoff:
                return BackupStats(backups_removed=removed)

            timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(current))
            target = directory / f"qqchat-backup-{timestamp}.db"
            partial = directory / f".qqchat-backup-{timestamp}.db.part"
            destination = sqlite3.connect(partial)
            try:
                self._db.backup(destination)
            finally:
                destination.close()
            partial.replace(target)
            target.touch()
            if now is not None:
                os.utime(target, (current, current))
            return BackupStats(
                created=True,
                bytes_written=target.stat().st_size,
                backups_removed=removed,
            )

    def close(self) -> None:
        self._db.close()
