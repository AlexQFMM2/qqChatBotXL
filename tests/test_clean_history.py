from __future__ import annotations

import sqlite3
import hashlib
import tempfile
import unittest
from pathlib import Path

from app.clean_history import run_cleanup


PNG = b"\x89PNG\r\n\x1a\nknown-emote"


class CleanHistoryTests(unittest.TestCase):
    def test_dry_run_then_apply_preserves_real_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "chat.db"
            workspace = root / "workspace"
            emotes = root / "emotes"
            backups = root / "backups"
            emotes.mkdir()
            (emotes / "one.png").write_bytes(PNG)
            group_key = hashlib.sha256(b"group").hexdigest()[:24]
            group = workspace / group_key / "inbox"
            group.mkdir(parents=True)
            polluted = group / "polluted.png"
            real = group / "real.png"
            polluted.write_bytes(PNG)
            real.write_bytes(b"\x89PNG\r\n\x1a\nreal-user-image")
            db = sqlite3.connect(database)
            db.executescript(
                """
                CREATE TABLE chat_messages(id INTEGER PRIMARY KEY, group_id TEXT, user_id TEXT,
                    username TEXT, content TEXT, is_bot INTEGER, created_at INTEGER);
                CREATE TABLE chat_attachments(id INTEGER PRIMARY KEY, chat_message_id INTEGER,
                    group_id TEXT, relative_path TEXT, content_type TEXT, created_at INTEGER);
                """
            )
            db.execute(
                "INSERT INTO chat_messages VALUES(1,'group','u','用户',?,0,1)",
                ("看图 [附件已保存：inbox/polluted.png] [附件已保存：inbox/real.png]",),
            )
            db.execute("INSERT INTO chat_messages VALUES(2,'group','bot','夏莉',?,1,2)",
                       ("<qqbot-at-user id='owner-openid'>x</qqbot-at-user>早安",))
            db.execute(
                "INSERT INTO chat_messages VALUES(3,'group','u','用户',?,0,3)",
                (
                    "[QQ 提供的前文上下文]\n=== 消息 1 ===\n"
                    "[附件1] 类型:图片 文件名:bot.png URL:[已由系统安全接收]\n"
                    "[当前消息]\n柚子社最新作是什么",
                ),
            )
            db.execute("INSERT INTO chat_attachments VALUES(1,1,'group','inbox/polluted.png','image/png',1)")
            db.execute("INSERT INTO chat_attachments VALUES(2,1,'group','inbox/real.png','image/png',1)")
            db.commit()
            db.close()

            preview = run_cleanup(database, workspace, emotes, owner_ids=("owner-openid",))
            self.assertEqual(preview.attachments, 1)
            self.assertEqual(preview.context_media_messages, 1)
            self.assertTrue(polluted.exists())
            applied = run_cleanup(
                database, workspace, emotes, owner_ids=("owner-openid",),
                apply=True, backup_dir=backups,
            )
            self.assertEqual(applied.attachments, 1)
            self.assertFalse(polluted.exists())
            self.assertTrue(real.exists())
            db = sqlite3.connect(database)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chat_attachments").fetchone()[0], 1)
            contents = [row[0] for row in db.execute("SELECT content FROM chat_messages ORDER BY id")]
            db.close()
            self.assertNotIn("polluted.png", contents[0])
            self.assertIn("real.png", contents[0])
            self.assertNotIn("owner-openid", contents[1])
            self.assertNotIn("类型:图片", contents[2])
            self.assertIn("柚子社最新作是什么", contents[2])
            self.assertTrue(list(backups.glob("pre-history-cleanup-*.db")))


if __name__ == "__main__":
    unittest.main()
