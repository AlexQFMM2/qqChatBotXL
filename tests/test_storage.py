from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from app.storage import MemoryStore


class AttachmentContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp.name) / "memory.db"))

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def test_recent_image_is_available_across_followup_messages(self) -> None:
        await self.store.add(
            "group",
            "user",
            "小明",
            "[图片]",
            attachments=(("inbox/picture.png", "image/png"),),
        )
        await self.store.add("group", "user", "小明", "看看刚才那张图")
        result = await self.store.recent_image_attachments("group", 20, 4)
        self.assertEqual(result, [("inbox/picture.png", "image/png")])

    async def test_image_falls_outside_message_window(self) -> None:
        await self.store.add(
            "group",
            "user",
            "小明",
            "[图片]",
            attachments=(("inbox/old.jpg", "image/jpeg"),),
        )
        for index in range(20):
            await self.store.add("group", "user", "小明", f"消息 {index}")
        result = await self.store.recent_image_attachments("group", 20, 4)
        self.assertEqual(result, [])

    async def test_reset_clears_attachment_context(self) -> None:
        await self.store.add(
            "group",
            "user",
            "小明",
            "[图片]",
            attachments=(("inbox/picture.webp", "image/webp"),),
        )
        await self.store.reset_group("group")
        self.assertEqual(await self.store.recent_image_attachments("group", 20, 4), [])

    async def test_global_and_group_sent_media_hashes_are_recognized(self) -> None:
        await self.store.record_sent_media("", "builtin-hash", "emote")
        await self.store.record_sent_media("group", "generated-hash", "generated_image")
        self.assertTrue(await self.store.is_recent_sent_media("group", "builtin-hash"))
        self.assertTrue(await self.store.is_recent_sent_media("group", "generated-hash"))
        self.assertFalse(await self.store.is_recent_sent_media("other", "generated-hash"))

    async def test_task_run_is_failed_after_restart(self) -> None:
        await self.store.create_task_run("one", "group", "message", "image")
        await self.store.start_task_run("one")
        self.assertEqual(
            await self.store.fail_stale_tasks(),
            [("one", "group", "message")],
        )

    async def test_cleanup_prunes_old_and_excess_chat_rows(self) -> None:
        now = 2_000_000_000
        with patch("app.storage.time.time", return_value=now):
            for index in range(5):
                await self.store.add(
                    "group",
                    "user",
                    "小明",
                    f"消息 {index}",
                    attachments=((f"inbox/{index}.png", "image/png"),),
                )
        self.store._db.execute(
            "INSERT INTO processed_messages(message_id, processed_at) VALUES (?, ?)",
            ("old-message", now - 8 * 86400),
        )
        self.store._db.commit()

        with patch("app.storage.time.time", return_value=now):
            stats = await self.store.cleanup(7, 30, 2)

        self.assertEqual(len(await self.store.history("group", 10)), 2)
        self.assertEqual(stats.chat_messages_removed, 3)
        self.assertEqual(stats.attachment_records_removed, 3)
        self.assertEqual(stats.processed_messages_removed, 1)

    async def test_database_backup_interval_and_retention(self) -> None:
        await self.store.add("group", "user", "小明", "需要备份")
        backup_dir = Path(self.temp.name) / "backups"
        now = 2_000_000_000

        first = await self.store.backup_if_due(
            str(backup_dir), 24, 7, now=now
        )
        second = await self.store.backup_if_due(
            str(backup_dir), 24, 7, now=now + 3600
        )
        third = await self.store.backup_if_due(
            str(backup_dir), 24, 7, now=now + 8 * 86400
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertTrue(third.created)
        self.assertEqual(third.backups_removed, 1)
        backups = list(backup_dir.glob("qqchat-backup-*.db"))
        self.assertEqual(len(backups), 1)
        database = sqlite3.connect(backups[0])
        try:
            self.assertEqual(
                database.execute("SELECT count(*) FROM chat_messages").fetchone()[0], 1
            )
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
