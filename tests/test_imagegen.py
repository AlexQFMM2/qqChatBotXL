from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import PersonaBot, _allowed_generated_image_url


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
                "self", "group"
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
                "self",
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
