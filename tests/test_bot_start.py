from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import PersonaBot


class BotIdentityStartupTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, get_me: AsyncMock) -> PersonaBot:
        bot = PersonaBot.__new__(PersonaBot)
        bot._qq = SimpleNamespace(get_me=get_me)
        bot._bot_user_id = ""
        bot._bot_username = ""
        bot._task_workers = []
        bot._settings = SimpleNamespace(task_queue_workers=0)
        bot._seed_builtin_media = AsyncMock()
        bot._store = SimpleNamespace(fail_stale_tasks=AsyncMock(return_value=[]))
        return bot

    async def test_caches_authenticated_bot_openid(self) -> None:
        bot = self.make_bot(
            AsyncMock(return_value={"id": "bot-openid", "username": "夏莉"})
        )
        await bot.start()
        self.assertEqual(bot._bot_user_id, "bot-openid")
        self.assertEqual(bot._bot_username, "夏莉")

    async def test_identity_network_failure_does_not_prevent_startup(self) -> None:
        bot = self.make_bot(AsyncMock(side_effect=asyncio.TimeoutError))
        await bot.start()
        self.assertEqual(bot._bot_user_id, "")
        bot._seed_builtin_media.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
