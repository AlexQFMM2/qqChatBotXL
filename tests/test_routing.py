from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

from app.bot import PersonaBot, TaskIntent, _references_image, _sha256_file
from app.domain import AttachmentRef
from app.llm import ImageInput
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

    def test_vision_requires_explicit_image_reference(self) -> None:
        self.assertTrue(_references_image("帮我看下这个图片"))
        self.assertTrue(_references_image("识别一下刚才的图"))
        self.assertFalse(_references_image("柚子社最新作是什么"))
        self.assertFalse(_references_image("生成一张图片"))


class VisionBackendRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = PersonaBot.__new__(PersonaBot)
        self.bot._llm = SimpleNamespace(complete=AsyncMock(return_value="客观观察"))
        self.image = ImageInput("image/png", b"\x89PNG\r\n\x1a\nimage")

    async def test_fusion_route_skips_direct_helper_and_forwards_images(self) -> None:
        self.bot._settings = SimpleNamespace(
            vision_route="fusion",
            vision_model="qwen-direct",
            vision_api_format="openai",
            llm_model="Fusion/qqchatbot",
        )

        prompt, images = await self.bot._route_vision("用户问题", [self.image])

        self.bot._llm.complete.assert_not_awaited()
        self.assertEqual(images, [self.image])
        self.assertIn("vision_understand", prompt)
        self.assertIn("不得根据文件名", prompt)

    async def test_direct_route_keeps_existing_helper_flow(self) -> None:
        self.bot._settings = SimpleNamespace(
            vision_route="direct",
            vision_model="qwen-direct",
            vision_api_format="openai",
            llm_model="Fusion/qqchatbot",
        )

        prompt, images = await self.bot._route_vision("用户问题", [self.image])

        self.bot._llm.complete.assert_awaited_once_with(
            ANY,
            "请输出供总控模型使用的中性视觉观察。",
            model="qwen-direct",
            images=[self.image],
            api_format="openai",
        )
        self.assertEqual(images, [])
        self.assertIn("客观观察", prompt)

    async def test_no_images_does_not_add_vision_instruction(self) -> None:
        self.bot._settings = SimpleNamespace(
            vision_route="fusion",
            llm_model="Fusion/qqchatbot",
        )

        prompt, images = await self.bot._route_vision("纯文字问题", [])

        self.bot._llm.complete.assert_not_awaited()
        self.assertEqual(prompt, "纯文字问题")
        self.assertEqual(images, [])


if __name__ == "__main__":
    unittest.main()
