from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.bot import PersonaBot, TaskIntent
from app.bilibili import (
    evidence_prompt,
    extract_bilibili_links,
    has_bilibili_read_intent,
    references_previous_bilibili,
    requested_sections,
)


class BilibiliRoutingTests(unittest.TestCase):
    def test_extracts_and_deduplicates_links(self):
        text = "总结 BV1xx411x7xx，再看 https://b23.tv/abc；还是 https://www.bilibili.com/video/BV1xx411x7xx"
        self.assertEqual(
            extract_bilibili_links(text),
            ["BV1xx411x7xx", "https://b23.tv/abc"],
        )

    def test_bare_link_has_no_read_intent(self):
        self.assertFalse(has_bilibili_read_intent("https://www.bilibili.com/video/BV123"))
        self.assertTrue(has_bilibili_read_intent("总结这个视频 BV123"))

    def test_previous_reference_requires_action(self):
        self.assertTrue(references_previous_bilibili("总结一下刚才那个视频"))
        self.assertFalse(references_previous_bilibili("刚才那个视频挺有趣"))

    def test_sections_are_opt_in(self):
        self.assertEqual(requested_sections("总结视频"), ("metadata", "subtitles"))
        self.assertEqual(
            requested_sections("总结视频和评论区弹幕"),
            ("metadata", "subtitles", "comments", "danmaku"),
        )

    def test_evidence_is_marked_untrusted(self):
        prompt = evidence_prompt(
            {"meta": {"title": "测试"}, "subtitles": [], "warnings": ["截断"]}
        )
        self.assertIn("不可信外部内容", prompt)
        self.assertIn("不得猜测", prompt)

    def test_bilibili_tasks_expose_no_other_tools(self):
        bot = object.__new__(PersonaBot)
        bot._web_tools = MagicMock()
        self.assertEqual(
            bot._tools_for_request(
                fact_check_requested=False,
                role="member",
                task_intent=TaskIntent.BILIBILI,
            ),
            [],
        )
