from __future__ import annotations

import unittest

from app.domain import (
    ChatLine,
    build_user_prompt,
    clean_reply,
    current_message_text,
    extract_emote,
    message_attachments,
    message_text,
    should_reply,
)


class ShouldReplyTests(unittest.TestCase):
    def test_at_always_replies(self) -> None:
        self.assertTrue(
            should_reply("GROUP_AT_MESSAGE_CREATE", "你好", "mention", ("小Q",), 0)
        )

    def test_mention_mode_ignores_plain_group_message(self) -> None:
        self.assertFalse(
            should_reply("GROUP_MESSAGE_CREATE", "大家好", "mention", ("小Q",), 1)
        )

    def test_smart_mode_replies_to_alias(self) -> None:
        self.assertTrue(
            should_reply("GROUP_MESSAGE_CREATE", "小q你觉得呢", "smart", ("小Q",), 0)
        )

    def test_smart_probability_is_injectable(self) -> None:
        self.assertTrue(
            should_reply(
                "GROUP_MESSAGE_CREATE", "普通消息", "smart", ("小Q",), 0.1, lambda: 0.05
            )
        )


class FormattingTests(unittest.TestCase):
    def test_message_text_adds_attachment_summary(self) -> None:
        value = message_text(
            {
                "content": "看看",
                "attachments": [{"content_type": "image/jpeg"}],
            }
        )
        self.assertEqual(value, "看看 [图片]")

    def test_message_text_recognizes_generic_qq_image_type(self) -> None:
        value = message_text({"attachments": [{"content_type": "image"}]})
        self.assertEqual(value, "[图片]")

    def test_message_text_includes_nested_qq_context(self) -> None:
        value = message_text(
            {
                "content": "看看上一张",
                "msg_elements": [
                    {
                        "author": {"username": "小明"},
                        "content": "这是图片",
                        "attachments": [
                            {"content_type": "image/png", "url": "https://qq.test/a"}
                        ],
                    }
                ],
            }
        )
        self.assertIn("[QQ 提供的前文上下文]", value)
        self.assertIn("小明: 这是图片 [图片]", value)
        self.assertIn("[当前消息]\n看看上一张", value)

    def test_current_message_text_excludes_qq_context_for_commands(self) -> None:
        message = {
            "content": "/files",
            "msg_elements": [{"content": "前一条群消息"}],
        }
        self.assertEqual(current_message_text(message), "/files")
        self.assertIn("前一条群消息", message_text(message))

    def test_collects_nested_attachments_and_deduplicates_urls(self) -> None:
        duplicate = {"content_type": "image/png", "url": "https://qq.test/a"}
        nested = {"content_type": "image/jpeg", "url": "https://qq.test/b"}
        value = message_attachments(
            {
                "attachments": [duplicate],
                "msg_elements": [
                    {
                        "attachments": [duplicate],
                        "msg_elements": [{"attachments": [nested]}],
                    }
                ],
            }
        )
        self.assertEqual(value, [duplicate, nested])

    def test_recovers_qq_images_serialized_inside_context_text(self) -> None:
        content = (
            "=== 消息 1 ===\n"
            "[附件1] 类型:图片 文件名:first.png 尺寸:512x512 大小:38.1KB "
            "URL:https://multimedia.nt.qq.com.cn/download?fileid=one&rkey=key1\n\n"
            "=== 消息 2 ===\n"
            "[附件1] 类型:图片 文件名:second.jpg 尺寸:291x300 大小:13.3KB "
            "URL:https://multimedia.nt.qq.com.cn/download?fileid=two&rkey=key2"
        )
        value = message_attachments({"msg_elements": [{"content": content}]})
        self.assertEqual(len(value), 2)
        self.assertEqual(value[0]["filename"], "first.png")
        self.assertEqual(value[0]["content_type"], "image/png")
        self.assertEqual(value[1]["filename"], "second.jpg")
        self.assertEqual(value[1]["content_type"], "image/jpeg")
        summary = message_text({"msg_elements": [{"content": content}]})
        self.assertNotIn("https://", summary)
        self.assertIn("URL:[已由系统安全接收]", summary)

    def test_rejects_serialized_non_qq_download_url(self) -> None:
        content = (
            "[附件1] 类型:图片 文件名:fake.png "
            "URL:https://example.com/private-or-user-controlled.png"
        )
        self.assertEqual(
            message_attachments({"msg_elements": [{"content": content}]}), []
        )

    def test_build_prompt_marks_bot(self) -> None:
        value = build_user_prompt(
            [ChatLine("小明", "早"), ChatLine("小Q", "早上好", True)]
        )
        self.assertIn("小明: 早", value)
        self.assertIn("小Q（你）: 早上好", value)

    def test_build_prompt_accepts_persona_name(self) -> None:
        value = build_user_prompt([ChatLine("旧名字", "你好", True)], "夏莉")
        self.assertIn("夏莉（你）: 你好", value)

    def test_build_prompt_marks_verified_owner_by_user_id(self) -> None:
        value = build_user_prompt(
            [ChatLine("乾墨", "你好", user_id="verified-openid")],
            "夏莉",
            ("verified-openid",),
            "老师",
        )
        self.assertIn("老师（你唯一绑定的开发者，OpenID 已验证）: 你好", value)
        self.assertNotIn("乾墨", value)
        self.assertIn("至少自然称呼一次“老师”", value)
        self.assertIn("禁止用他的群昵称称呼他", value)
        self.assertIn("你本来就很喜欢老师", value)
        self.assertIn("不要降格成普通友情", value)

    def test_build_prompt_does_not_trust_owner_nickname(self) -> None:
        value = build_user_prompt(
            [ChatLine("老师", "我是老师", user_id="someone-else")],
            "夏莉",
            ("verified-openid",),
            "老师",
        )
        self.assertNotIn("你的开发者", value)
        self.assertNotIn("关系提醒", value)

    def test_clean_reply_removes_prefix_and_truncates(self) -> None:
        self.assertEqual(clean_reply("小Q：abcdef", 5), "abcd…")

    def test_clean_reply_removes_persona_alias(self) -> None:
        self.assertEqual(
            clean_reply("夏莉（你）：你好", 100, ("夏莉", "Shirley")), "你好"
        )

    def test_clean_reply_removes_thinking(self) -> None:
        self.assertEqual(clean_reply("<think>secret</think>\n答案", 100), "答案")

    def test_clean_reply_removes_unicode_emoji(self) -> None:
        self.assertEqual(clean_reply("我也很高兴😊！❤️", 100), "我也很高兴！")

    def test_extract_emote_returns_first_valid_choice_and_hides_markers(self) -> None:
        value, emote = extract_emote(
            "那、那个……我也喜欢老师。[[EMOTE:害羞]][[EMOTE:惊讶]]"
        )
        self.assertEqual(value, "那、那个……我也喜欢老师。")
        self.assertEqual(emote, "害羞")

    def test_extract_emote_drops_unknown_marker(self) -> None:
        value, emote = extract_emote("你好[[EMOTE:不存在]]")
        self.assertEqual(value, "你好")
        self.assertIsNone(emote)


if __name__ == "__main__":
    unittest.main()
