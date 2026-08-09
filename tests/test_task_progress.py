from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from app.bot import PersonaBot, QueuedTask, _queued_eta, _task_eta


class TaskProgressTests(unittest.TestCase):
    def test_estimates_common_task_types(self) -> None:
        self.assertEqual(_task_eta("帮我查一下上海天气"), "1 分钟内")
        self.assertEqual(_task_eta("帮我写一份简单说明"), "1～3 分钟")
        self.assertEqual(_task_eta("整理成一份完整的 PDF 报告"), "2～5 分钟")
        self.assertEqual(_task_eta("请用语音回复我"), "1～3 分钟")

    def test_does_not_ack_chat_or_capability_questions(self) -> None:
        self.assertIsNone(_task_eta("我喜欢你"))
        self.assertIsNone(_task_eta("你能不能生成图片？"))

    def test_queue_expands_estimate(self) -> None:
        self.assertEqual(_queued_eta("1 分钟内"), "2～6 分钟")
        self.assertEqual(_queued_eta("2～5 分钟"), "4～10 分钟")
        self.assertEqual(_queued_eta("2～5 分钟", 2), "6～15 分钟")


class TaskQueueTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, *, size: int = 10, workers: int = 2) -> PersonaBot:
        bot = PersonaBot.__new__(PersonaBot)
        bot._settings = SimpleNamespace(  # type: ignore[attr-defined]
            task_queue_size=size,
            task_queue_workers=workers,
        )
        bot._task_groups = {}
        bot._task_scheduled_groups = set()
        bot._task_active_groups = set()
        bot._task_ready_groups = asyncio.Queue(maxsize=size)
        bot._task_workers = [object()]  # type: ignore[list-item]
        bot._task_outstanding = 0
        return bot

    async def test_same_group_reports_position_and_is_scheduled_once(self) -> None:
        bot = self.make_bot()
        first = QueuedTask("event", {}, "group", "one")
        second = QueuedTask("event", {}, "group", "two")

        self.assertEqual(bot._enqueue_task(first), 0)
        self.assertEqual(bot._enqueue_task(second), 1)
        self.assertEqual(bot.task_queue_size, 2)
        self.assertEqual(bot._task_ready_groups.qsize(), 1)

    async def test_full_queue_rejects_instead_of_dropping_task(self) -> None:
        bot = self.make_bot(size=1)
        bot._enqueue_task(QueuedTask("event", {}, "group", "one"))

        with self.assertRaises(asyncio.QueueFull):
            bot._enqueue_task(QueuedTask("event", {}, "other", "two"))


if __name__ == "__main__":
    unittest.main()
