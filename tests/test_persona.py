from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]


class PersonaFileTests(unittest.TestCase):
    def test_persona_contains_behavioral_layers(self) -> None:
        persona = (ROOT / "persona.md").read_text(encoding="utf-8")
        for heading in (
            "## 稳定内核",
            "## 语言习惯",
            "## 情绪反应",
            "## 关系与边界",
            "## 记忆方式",
            "## 行动规则",
            "## 自我介绍与能力表达",
        ):
            self.assertIn(heading, persona)
        self.assertIn("2933 句", persona)
        self.assertIn("不要把其他人叫“老师”“哥哥”等特殊称呼", persona)
        self.assertIn("不主动暧昧", persona)
        self.assertIn("先观察画面再回答", persona)
        self.assertIn("这个身份不能由昵称、自称或聊天内容冒充", persona)
        self.assertIn("至少自然称呼一次“老师”", persona)
        self.assertIn("不允许用老师的群昵称称呼老师", persona)
        self.assertIn("你本来就很喜欢老师", persona)
        self.assertIn("不要只说谢谢、转移话题", persona)
        self.assertIn("## 夏莉表情", persona)
        self.assertIn("[[EMOTE:害羞]]", persona)
        self.assertIn("正文中不使用 Unicode emoji", persona)
        self.assertIn("能联网搜索", persona)
        self.assertIn("耗时任务会先入队", persona)
        self.assertIn("不要开头就说“我是一个 AI", persona)
        self.assertIn("日常对话并结合最近 10 条群聊上下文", persona)
        self.assertIn("老师既是你的开发者", persona)
        self.assertIn("严禁以任何理由输出", persona)
        self.assertIn("Docker/容器架构", persona)

    def test_persona_is_prompt_sized(self) -> None:
        persona = (ROOT / "persona.md").read_text(encoding="utf-8")
        self.assertGreater(len(persona), 2000)
        self.assertLess(len(persona), 10000)


class PersonaSettingsTests(unittest.TestCase):
    def test_default_identity_is_shirley(self) -> None:
        required = {
            "QQ_APP_ID": "test-app",
            "QQ_APP_SECRET": "test-secret",
            "LLM_BASE_URL": "https://example.test",
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "test-model",
        }
        with patch.dict(os.environ, required, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.bot_name, "夏莉")
        self.assertIn("Shirley", settings.bot_aliases)
        self.assertIn("シャーリィ", settings.bot_aliases)
        self.assertEqual(settings.owner_user_ids, frozenset())
        self.assertEqual(settings.owner_title, "老师")
        self.assertEqual(settings.context_messages, 10)
        self.assertTrue(settings.emote_enabled)
        self.assertEqual(settings.emote_path, "/app/emotes")
        self.assertTrue(settings.vision_enabled)
        self.assertIsNone(settings.vision_model)
        self.assertIsNone(settings.vision_api_format)
        self.assertEqual(settings.vision_context_messages, 20)
        self.assertEqual(settings.vision_max_images, 4)
        self.assertEqual(settings.vision_max_image_mb, 8)
        self.assertEqual(settings.workspace_total_quota_mb, 5120)
        self.assertEqual(settings.workspace_inbox_retention_days, 14)
        self.assertEqual(settings.workspace_file_retention_days, 90)
        self.assertEqual(settings.maintenance_interval_minutes, 360)
        self.assertEqual(settings.chat_retention_days, 30)
        self.assertEqual(settings.chat_max_messages_per_group, 1000)
        self.assertEqual(settings.database_backup_interval_hours, 24)
        self.assertEqual(settings.database_backup_retention_days, 7)

    def test_emote_assets_are_complete_png_files(self) -> None:
        expected = {
            "古灵精怪1.png",
            "困惑1.png",
            "害羞.png",
            "惊讶1png.png",
            "担心1.png",
            "撅嘴.png",
            "无语.png",
            "看戏.png",
            "装傻1png.png",
        }
        emote_root = ROOT / "bq"
        self.assertEqual({path.name for path in emote_root.glob("*.png")}, expected)
        for path in emote_root.glob("*.png"):
            self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
