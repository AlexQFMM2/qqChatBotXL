from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.scheduler import GoodMorningScheduler, LAST_GOOD_MORNING_KEY
from app.storage import MemoryStore


BEIJING = ZoneInfo("Asia/Shanghai")


class FakeQQ:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_group_text(
        self, group_id: str, content: str, message_id: str | None = None
    ) -> dict:
        self.messages.append((group_id, content))
        return {"id": "sent"}


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        good_morning_enabled=True,
        good_morning_groups=frozenset({"group-1"}),
        good_morning_hour=7,
        good_morning_minute=0,
        owner_user_ids=frozenset({"owner-openid"}),
        owner_title="老师",
        bot_name="夏莉",
    )


class GoodMorningSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temporary.name) / "test.db"))
        self.qq = FakeQQ()
        self.scheduler = GoodMorningScheduler(settings(), self.qq, self.store)  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    async def test_sends_mention_once_per_day_and_persists_it(self) -> None:
        day = date(2026, 8, 10)
        self.assertEqual(await self.scheduler.send_for_date(day), 1)
        self.assertEqual(await self.scheduler.send_for_date(day), 0)
        self.assertEqual(len(self.qq.messages), 1)
        self.assertIn('<qqbot-at-user id="owner-openid" />', self.qq.messages[0][1])
        self.assertIn("老师，早安", self.qq.messages[0][1])
        self.assertEqual(
            await self.store.get_setting("group-1", LAST_GOOD_MORNING_KEY),
            "2026-08-10",
        )

    async def test_next_target_uses_beijing_time(self) -> None:
        before = datetime(2026, 8, 9, 6, 59, tzinfo=BEIJING)
        after = datetime(2026, 8, 9, 7, 1, tzinfo=BEIJING)
        self.assertEqual(
            self.scheduler.next_target(before),
            datetime(2026, 8, 9, 7, 0, tzinfo=BEIJING),
        )
        self.assertEqual(
            self.scheduler.next_target(after),
            datetime(2026, 8, 10, 7, 0, tzinfo=BEIJING),
        )


if __name__ == "__main__":
    unittest.main()
