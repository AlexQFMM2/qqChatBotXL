from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import PersonaBot, _allowed_generated_image_url


class ExplicitImageRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = PersonaBot.__new__(PersonaBot)
        self.bot._settings = SimpleNamespace(bot_aliases=("夏莉", "Shirley"))

    def test_accepts_explicit_natural_request(self) -> None:
        self.assertEqual(
            self.bot._explicit_image_prompt("夏莉，帮我生成一张图片 一只白猫"),
            "一只白猫",
        )
        self.assertEqual(self.bot._explicit_image_prompt("画一张星空"), "星空")
        self.assertEqual(
            self.bot._explicit_image_prompt("可以，生成四格漫画"), "四格漫画"
        )

    def test_accepts_slash_command(self) -> None:
        self.assertEqual(self.bot._explicit_image_prompt("/image watercolor fox"), "watercolor fox")

    def test_does_not_trigger_on_capability_question(self) -> None:
        self.assertIsNone(self.bot._explicit_image_prompt("你能生成图片吗？"))
        self.assertIsNone(self.bot._explicit_image_prompt("生成一份 PDF 报告"))
        self.assertIsNone(self.bot._explicit_image_prompt("生成一条语音"))

    def test_accepts_self_and_chat_reference_requests(self) -> None:
        self.assertEqual(
            self.bot._explicit_image_prompt("以你自己为原型，画一张海边插画"),
            "以你自己为原型，画一张海边插画",
        )
        self.assertEqual(
            self.bot._explicit_image_prompt("参考刚才的图，把她画成水彩风格"),
            "参考刚才的图，把她画成水彩风格",
        )

    def test_does_not_treat_reference_capability_question_as_request(self) -> None:
        self.assertIsNone(self.bot._explicit_image_prompt("能不能参考这张图画画？"))


class ImageEditReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_curated_character_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one.png", "two.png", "three.png"):
                (root / name).write_bytes(b"\x89PNG\r\n\x1a\nreference")
            bot = PersonaBot.__new__(PersonaBot)
            bot._settings = SimpleNamespace(
                image_edit_enabled=True,
                image_edit_max_images=2,
                image_character_references=("one.png", "two.png", "three.png"),
                emote_path=str(root),
                vision_max_image_mb=8,
            )

            images = await bot._image_edit_inputs(
                "以你自己为原型，画一张海边插画", "group"
            )

        self.assertEqual(len(images), 2)
        self.assertTrue(all(image.media_type == "image/png" for image in images))

    async def test_self_reference_wording_does_not_mix_old_chat_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one.png", "two.png", "three.png"):
                (root / name).write_bytes(b"\x89PNG\r\n\x1a\nreference")
            bot = PersonaBot.__new__(PersonaBot)
            bot._settings = SimpleNamespace(
                image_edit_enabled=True,
                image_edit_max_images=3,
                image_character_references=("one.png", "two.png", "three.png"),
                emote_path=str(root),
                vision_max_image_mb=8,
            )
            bot._recent_image_attachments = AsyncMock(
                side_effect=AssertionError("不应读取聊天旧图")
            )

            images = await bot._image_edit_inputs(
                "以你自己为参考，人物长相服装与参考图完全一致，画一张三格漫画",
                "group",
            )

        self.assertEqual(len(images), 3)
        bot._recent_image_attachments.assert_not_awaited()


class GeneratedImageUrlTests(unittest.TestCase):
    def test_allows_dashscope_oss_https(self) -> None:
        self.assertTrue(
            _allowed_generated_image_url(
                "https://dashscope-a717.oss-accelerate.aliyuncs.com/output.png"
            )
        )

    def test_rejects_untrusted_or_non_https_urls(self) -> None:
        self.assertFalse(_allowed_generated_image_url("http://dashscope.aliyuncs.com/a.png"))
        self.assertFalse(_allowed_generated_image_url("https://example.com/a.png"))
        self.assertFalse(
            _allowed_generated_image_url(
                "https://dashscope.aliyuncs.com.example.com/a.png"
            )
        )


if __name__ == "__main__":
    unittest.main()
