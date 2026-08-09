from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from app.image_text import apply_text_overlays, plan_image_text


FONT_PATH = Path("assets/fonts/DroidSansFallbackFull.ttf")


class ImageTextPlanTests(unittest.TestCase):
    def test_extracts_only_requested_visible_text(self) -> None:
        plan = plan_image_text(
            "生成“一张三格漫画”。第一格配有“！”符号，第二格有“噗～”的拟声词。"
            "第三格左边对话框写着“你觉得我漂亮”，右侧下方写着“那是因为你爱上我了”。"
        )

        self.assertEqual(
            plan.texts,
            ("！", "噗～", "你觉得我漂亮", "那是因为你爱上我了"),
        )
        self.assertEqual(
            tuple(item.position for item in plan.items),
            ("top_left", "top_right", "bottom_left", "bottom_right"),
        )
        self.assertIn("一张三格漫画", plan.model_prompt)
        self.assertNotIn("你觉得我漂亮", plan.model_prompt)
        self.assertIn("禁止在图中生成任何汉字", plan.model_prompt)

    def test_leaves_non_text_quotes_alone(self) -> None:
        plan = plan_image_text("生成“一张三格漫画”，人物微笑。")
        self.assertEqual(plan.texts, ())
        self.assertEqual(plan.model_prompt, "生成“一张三格漫画”，人物微笑。")


class ImageTextOverlayTests(unittest.TestCase):
    def test_renders_exact_chinese_into_detected_blank_regions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comic.png"
            image = Image.new("RGB", (800, 500), (80, 120, 180))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((40, 40, 340, 180), radius=30, fill="white", outline="black", width=5)
            draw.rounded_rectangle((460, 280, 760, 440), radius=30, fill="white", outline="black", width=5)
            image.save(path)

            result = apply_text_overlays(
                path,
                ("你觉得我漂亮", "那是因为你爱上我了"),
                FONT_PATH,
            )

            with Image.open(path) as rendered:
                self.assertEqual(rendered.format, "PNG")
                self.assertEqual(rendered.size, (800, 500))
                dark_pixels = sum(
                    1
                    for red, green, blue in rendered.crop((80, 70, 300, 150)).getdata()
                    if red + green + blue < 300
                )
                self.assertGreater(dark_pixels, 100)
        self.assertEqual(result.text_count, 2)
        self.assertEqual(result.detected_regions, 2)
        self.assertFalse(result.used_fallback)


if __name__ == "__main__":
    unittest.main()
