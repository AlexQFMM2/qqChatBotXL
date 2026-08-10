from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.bot import PersonaBot, TaskIntent, _sha256_file
from app.domain import AttachmentRef
from app.storage import MemoryStore
from app.workspace import GroupWorkspace


class FakeQQ:
    async def download_attachment(self, _url: str, target: Path, _maximum: int) -> int:
        target.write_bytes(b"\x89PNG\r\n\x1a\nrobot-emote")
        return target.stat().st_size


class AttachmentRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bot = PersonaBot.__new__(PersonaBot)
        self.bot._workspace = GroupWorkspace(str(root / "workspace"), 10, 1000, 50, 100)
        self.bot._store = MemoryStore(str(root / "chat.db"))
        self.bot._qq = FakeQQ()

    async def asyncTearDown(self) -> None:
        self.bot._store.close()
        self.temp.cleanup()

    async def test_bot_context_is_never_downloaded(self) -> None:
        values = await self.bot._save_attachments(
            [AttachmentRef({"url": "https://qq.test/a", "content_type": "image/png"}, "bot_context")],
            "group", "message",
        )
        self.assertEqual(values, [])

    async def test_unknown_context_matching_sent_media_is_discarded(self) -> None:
        probe = Path(self.temp.name) / "probe.png"
        probe.write_bytes(b"\x89PNG\r\n\x1a\nrobot-emote")
        await self.bot._store.record_sent_media("", _sha256_file(probe), "emote")
        values = await self.bot._save_attachments(
            [AttachmentRef({"url": "https://qq.test/a", "content_type": "image/png"}, "unknown_context")],
            "group", "message",
        )
        self.assertEqual(values, [])

    async def test_unknown_context_matching_outbound_message_id_is_not_downloaded(self) -> None:
        await self.bot._store.record_sent_media(
            "group", "different-hash", "emote", "bot-message-id"
        )
        values = await self.bot._save_attachments(
            [
                AttachmentRef(
                    {"url": "https://qq.test/a", "content_type": "image/png"},
                    "unknown_context",
                    "bot-message-id",
                )
            ],
            "group",
            "message",
        )
        self.assertEqual(values, [])

    async def test_explicit_current_user_upload_is_kept_even_if_hash_matches(self) -> None:
        probe = Path(self.temp.name) / "probe.png"
        probe.write_bytes(b"\x89PNG\r\n\x1a\nrobot-emote")
        await self.bot._store.record_sent_media("", _sha256_file(probe), "emote")
        values = await self.bot._save_attachments(
            [AttachmentRef({"url": "https://qq.test/a", "content_type": "image/png"}, "current_user")],
            "group", "message",
        )
        self.assertEqual(len(values), 1)


class ControllerIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = PersonaBot.__new__(PersonaBot)
        self.bot._settings = SimpleNamespace(
            bot_aliases=("夏莉",), agent_access="admins", owner_user_ids=frozenset()
        )
        self.bot._web_tools = object()

    def test_only_bound_work_is_queued(self) -> None:
        self.assertEqual(
            self.bot._task_intent("我喜欢你", image_prompt=None, voice_requested=False),
            TaskIntent.CHAT,
        )
        prompt = self.bot._explicit_image_prompt("可以，生成四格漫画")
        self.assertEqual(
            self.bot._task_intent("可以，生成四格漫画", image_prompt=prompt, voice_requested=False),
            TaskIntent.IMAGE,
        )
        self.assertEqual(
            self.bot._task_intent("查资料：夏莉出自哪部作品", image_prompt=None, voice_requested=False),
            TaskIntent.SEARCH,
        )

    def test_prefetched_fact_query_does_not_expose_search_tools_again(self) -> None:
        tools = self.bot._tools_for_request(
            fact_check_requested=True,
            role="member",
            task_intent=TaskIntent.SEARCH,
        )
        self.assertEqual(tools, [])

    def test_latest_work_is_forced_into_research(self) -> None:
        self.assertEqual(
            self.bot._task_intent(
                "柚子社最新作是什么", image_prompt=None, voice_requested=False
            ),
            TaskIntent.SEARCH,
        )


if __name__ == "__main__":
    unittest.main()
