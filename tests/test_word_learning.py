from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.channel import IncomingMessage, TransportError
from app.llm import FinalToolReply, LLMClient, LLMError
from app.wechat.client import DeliveryUnknown
from app.word_learning import WORD_CARD_TOOL, WordCardError, check_requested_word, parse_word_card
from app.workspace import WorkspaceError
import test_wechat_chat as wechat_tests


def sample_card(**changes):
    return {
        "word": "unhappy", "ipa_uk": "/ʌnˈhæpi/", "ipa_us": "/ʌnˈhæpi/",
        "meanings": [{"part_of_speech": "adj. 形容词", "translation": "不开心的；不满意的"}],
        "formation": "un-（否定前缀）＋ happy（开心的）→ 不开心的。",
        "example_en": "She was unhappy with the result.", "example_zh": "她对这个结果不满意。",
        **changes,
    }


class WordCardTests(unittest.TestCase):
    def test_readable_sections_and_ipa_survive_without_truncation(self):
        card = parse_word_card(sample_card())
        expected = (
            "unhappy\n\n音标\n英 /ʌnˈhæpi/ ｜ 美 /ʌnˈhæpi/"
            "\n\n翻译\n不开心的；不满意的\n\n词性\nadj. 形容词"
            "\n\n构词拆解\nun-（否定前缀）＋ happy（开心的）→ 不开心的。"
            "\n\n例句\nShe was unhappy with the result.\n她对这个结果不满意。"
        )
        self.assertEqual(card.render(max_chars=len(expected)), expected)
        with self.assertRaisesRegex(WordCardError, "不能截断"):
            card.render(max_chars=len(expected) - 1)
        with self.assertRaises(WordCardError):
            card.render(max_chars=len(expected), voice_note="语音未启用")

    def test_multiple_senses_have_matching_numbered_meanings_and_parts(self):
        card = parse_word_card(sample_card(word="record", ipa_uk="n. /ˈrekɔːd/；v. /rɪˈkɔːd/",
            meanings=[{"part_of_speech": "n. 名词", "translation": "记录"},
                      {"part_of_speech": "v. 动词", "translation": "记录；录制"}]))
        text = card.render(max_chars=1500)
        self.assertIn("翻译\n1. 记录\n2. 记录；录制", text)
        self.assertIn("词性\n1. n. 名词\n2. v. 动词", text)
        self.assertIn("n. /ˈrekɔːd/；v. /rɪˈkɔːd/", text)

    def test_personal_opening_does_not_change_structured_explanation(self):
        original = parse_word_card(sample_card()).render(max_chars=1500)
        opening = "老师，这个词拆开就好记啦，我们一起看。"
        text = parse_word_card(sample_card(opening=opening)).render(max_chars=1500)
        self.assertEqual(text, opening + "\n\n" + original)
        self.assertEqual(parse_word_card(sample_card(opening="")).render(max_chars=1500), original)
        for value in (None, False, "x" * 81, "两行\n开场", "[[EMOTE:害羞]]"):
            with self.subTest(value=value), self.assertRaises(WordCardError):
                parse_word_card(sample_card(opening=value))
        with self.assertRaises(WordCardError):
            parse_word_card(sample_card(opening=opening)).render(max_chars=len(original))

    def test_unknown_phonetics_and_no_structure_are_explicit(self):
        text = parse_word_card(sample_card(word="apple", ipa_uk="未确认", ipa_us="未确认",
            formation="无明显可拆分结构")).render(max_chars=1500)
        self.assertIn("英 未确认 ｜ 美 未确认", text)
        self.assertIn("构词拆解\n无明显可拆分结构", text)

    def test_valid_word_forms_and_default_pronunciation(self):
        for word in ("reusable", "Apple", "mother-in-law", "can't", "can’t"):
            with self.subTest(word=word):
                card = parse_word_card(sample_card(word=word))
                self.assertEqual(card.word, word.replace("’", "'"))
                self.assertTrue(card.pronounce)
        self.assertFalse(parse_word_card(sample_card(pronounce=False)).pronounce)

    def test_rejects_bad_types_unknown_fields_empty_content_and_control_markers(self):
        cases = [sample_card(word=word) for word in ("two words", "http://example.com", "../word", "hello1", "你好", "a" * 65)]
        cases += [sample_card(**change) for change in (
            {"pronounce": "false"}, {"pronounce": 0}, {"ipa_uk": None}, {"formation": " "},
            {"example_en": "hello\nworld"}, {"example_en": "hello\u200bworld"}, {"example_en": "hello\u2028world"},
            {"formation": "[[EMOTE:害羞]]"}, {"example_zh": "<think>内容</think>"},
            {"ipa_us": "a" * 181}, {"url": "https://example.com"}, {"meanings": {}},
            {"meanings": []}, {"meanings": [{}]},
            {"meanings": [{"part_of_speech": "n.", "translation": "词", "extra": "x"}]},
            {"meanings": [{"part_of_speech": "n.", "translation": "词"}] * 4},
        )]
        for field in WORD_CARD_TOOL["input_schema"]["required"]:
            value = sample_card()
            del value[field]
            cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(WordCardError):
                parse_word_card(value)

    def test_does_not_silently_correct_spelling_but_accepts_confirmed_context(self):
        card = parse_word_card(sample_card(word="receive"))
        with self.assertRaisesRegex(WordCardError, "不能默默改词"):
            check_requested_word(card, "解读 recieve")
        check_requested_word(card, "就按你说的正确拼写来解读")
        check_requested_word(card, "解读 Receive")


class FinalToolReplyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def settings(api_format):
        return SimpleNamespace(llm_api_format=api_format, llm_base_url="https://example.test",
            llm_api_key="fake", llm_model="model", llm_max_tokens=1200, llm_temperature=0.5)

    async def test_both_protocols_stop_before_more_tools_or_model_requests(self):
        for api_format in ("anthropic", "openai"):
            with self.subTest(api_format=api_format):
                if api_format == "anthropic":
                    response = {"content": [
                        {"type": "tool_use", "id": "one", "name": "submit_word_card", "input": {}},
                        {"type": "tool_use", "id": "two", "name": "send_voice", "input": {"text": "duplicate"}},
                    ]}
                else:
                    response = {"choices": [{"message": {"tool_calls": [
                        {"id": "one", "function": {"name": "submit_word_card", "arguments": "{}"}},
                        {"id": "two", "function": {"name": "send_voice", "arguments": "{}"}},
                    ]}}]}
                llm = LLMClient(self.settings(api_format), None)
                llm._post = AsyncMock(return_value=response)
                execute = AsyncMock(return_value=FinalToolReply("完整卡片"))
                result = await llm.complete_with_tools("rules", "word", [WORD_CARD_TOOL], execute)
                self.assertEqual(result, "完整卡片")
                execute.assert_awaited_once_with("submit_word_card", {})
                llm._post.assert_awaited_once()

    async def test_invalid_card_can_be_corrected_in_both_protocols(self):
        import json
        for api_format in ("anthropic", "openai"):
            with self.subTest(api_format=api_format):
                responses = []
                for index, arguments in enumerate(({}, sample_card())):
                    if api_format == "anthropic":
                        response = {"content": [{"type": "tool_use", "id": str(index),
                            "name": "submit_word_card", "input": arguments}]}
                    else:
                        response = {"choices": [{"message": {"tool_calls": [{"id": str(index),
                            "function": {"name": "submit_word_card", "arguments": json.dumps(arguments)}}]}}]}
                    responses.append(response)
                llm = LLMClient(self.settings(api_format), None)
                llm._post = AsyncMock(side_effect=responses)
                async def execute(name, arguments):
                    return FinalToolReply(parse_word_card(arguments).render(max_chars=1500))
                result = await llm.complete_with_tools("rules", "word", [WORD_CARD_TOOL], execute)
                self.assertTrue(result.startswith("unhappy\n"))
                self.assertEqual(llm._post.await_count, 2)


class WordCardControllerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wechat_tests.WeChatCoreTests.asyncSetUp
    asyncTearDown = wechat_tests.WeChatCoreTests.asyncTearDown
    process = wechat_tests.WeChatCoreTests.process
    sent_text = wechat_tests.WeChatCoreTests.sent_text

    def prepare(self, *, card=None, enabled=True):
        self.bot._settings = replace(self.settings, voice_enabled=enabled)
        self.llm.generate_voice = AsyncMock(return_value=b"validated-audio")
        self.transport.media.upload = AsyncMock(return_value={"type": 4, "file_item": {"file_name": "word.wav"}})
        async def controller(system, prompt, tools, execute, **kwargs):
            self.assertIn("英语单词解读", system)
            self.assertIn("submit_word_card", {tool["name"] for tool in tools})
            result = await execute("submit_word_card", sample_card() if card is None else card)
            self.assertIsInstance(result, FinalToolReply)
            return result.text
        self.llm.complete_with_tools.side_effect = controller

    def memory_guard(self):
        from contextlib import nullcontext
        memory = SimpleNamespace(turn=lambda _: nullcontext(), prompt=lambda _: "", valid=lambda: True,
            file_refs=lambda *args, **kwargs: True, safe_reply=lambda text: text)
        self.bot._memory = memory
        return memory

    async def test_wechat_card_precedes_single_word_audio_and_no_freeform_or_emote(self):
        self.prepare()
        async def synthesize(*args, **kwargs):
            self.assertEqual(self.client.send.await_count, 1)
            self.assertIn("构词拆解", self.sent_text())
            return b"audio"
        self.llm.generate_voice.side_effect = synthesize
        await self.process(wechat_tests.message("解读 unhappy"))
        self.llm.generate_voice.assert_awaited_once()
        self.assertEqual(self.llm.generate_voice.await_args.args, ("unhappy",))
        self.assertEqual(self.llm.generate_voice.await_args.kwargs["audio_format"], "wav")
        self.assertEqual(self.llm.generate_voice.await_args.kwargs["mode"], "word")
        self.assertEqual([call.args[2]["type"] for call in self.client.send.await_args_list], [1, 4])
        self.assertNotIn("正在生成语音", self.sent_text())
        history = await self.store.history(self.transport.conversation_id, 10)
        self.assertEqual([line.content for line in history if line.is_bot],
                         [parse_word_card(sample_card()).render(max_chars=1500), "[语音转写] unhappy"])

    async def test_qq_card_precedes_silk_and_sequences_are_distinct(self):
        self.prepare()
        order = []
        async def send_text(*args, **kwargs):
            order.append(("text", kwargs["sequence"]))
            return {"id": "text"}
        async def send_file(*args, **kwargs):
            order.append(("voice", kwargs["sequence"]))
            return {"id": "voice"}
        self.bot._channel_transport = SimpleNamespace(send_text=send_text, send_file=send_file)
        await self.bot.handle_message(IncomingMessage("qq", "group", "message", "owner", "老师", "owner",
            "解读 unhappy", "解读 unhappy"), _from_task_queue=True)
        self.assertEqual(order, [("text", 1), ("voice", 2)])
        self.assertEqual(self.llm.generate_voice.await_args.kwargs["audio_format"], "silk")
        self.assertEqual(self.llm.generate_voice.await_args.kwargs["mode"], "word")

    async def test_opening_is_sent_once_before_standard_card_then_word_audio(self):
        opening = "这个词其实挺好拆的，老师看这里。"
        self.prepare(card=sample_card(opening=opening))
        await self.process(wechat_tests.message("解读 unhappy"))
        self.assertTrue(self.sent_text().startswith(opening + "\n\nunhappy\n\n音标\n"))
        self.assertEqual(self.sent_text().count(opening), 1)
        self.assertEqual([c.args[2]["type"] for c in self.client.send.await_args_list], [1, 4])
        self.assertEqual(self.llm.generate_voice.await_args.args, ("unhappy",))

    async def test_explicit_text_only_has_no_voice_note_or_paid_call(self):
        self.prepare(card=sample_card(pronounce=False))
        await self.process(wechat_tests.message("解读 unhappy，只要文字"))
        self.llm.generate_voice.assert_not_awaited()
        self.assertNotIn("读音", self.sent_text())
        self.client.send.assert_awaited_once()

    async def test_disabled_voice_preserves_complete_card_with_note(self):
        self.prepare(enabled=False)
        await self.process(wechat_tests.message("解读 unhappy"))
        self.llm.generate_voice.assert_not_awaited()
        self.assertIn("例句", self.sent_text())
        self.assertIn("未启用语音", self.sent_text())

    async def test_voice_length_limit_skips_synthesis_without_losing_card(self):
        self.prepare()
        self.bot._settings = replace(self.bot._settings, voice_max_chars=5)
        await self.process(wechat_tests.message("解读 unhappy"))
        self.llm.generate_voice.assert_not_awaited()
        self.assertIn("朗读长度限制", self.sent_text())
        self.assertIn("例句", self.sent_text())

    async def test_member_gets_text_without_voice_permission_escalation(self):
        self.prepare()
        self.transport.bind(wechat_tests.message("解读 unhappy"))
        message, _ = self.transport.normalize("老师")
        message = replace(message, role="member", user_id="impostor")
        await self.bot.handle_message(message, _from_task_queue=True)
        self.assertIn("当前身份未开放语音", self.sent_text())
        self.assertIn("构词拆解", self.sent_text())
        self.llm.generate_voice.assert_not_awaited()

    async def test_everyone_setting_allows_member_word_voice(self):
        self.prepare()
        self.bot._settings = replace(self.bot._settings, agent_access="everyone")
        self.transport.bind(wechat_tests.message("解读 unhappy"))
        message, _ = self.transport.normalize("老师")
        await self.bot.handle_message(replace(message, role="member", user_id="member"), _from_task_queue=True)
        self.llm.generate_voice.assert_awaited_once()

    async def test_duplicate_incoming_message_has_no_second_paid_call(self):
        self.prepare()
        message = wechat_tests.message("解读 unhappy")
        await self.process(message)
        await self.process(message)
        self.llm.complete_with_tools.assert_awaited_once()
        self.llm.generate_voice.assert_awaited_once()

    async def test_duplicate_submission_is_idempotent_and_blocks_further_tools(self):
        self.prepare()
        async def controller(system, prompt, tools, execute, **kwargs):
            first = await execute("submit_word_card", sample_card())
            self.assertEqual(first, await execute("submit_word_card", sample_card()))
            for name, arguments in (("send_voice", {"text": "duplicate"}),
                                    ("submit_word_card", sample_card(word="apple"))):
                with self.assertRaises(WorkspaceError):
                    await execute(name, arguments)
            return "不应发送的自由文本 [[EMOTE:害羞]]"
        self.llm.complete_with_tools.side_effect = controller
        await self.process(wechat_tests.message("解读 unhappy"))
        self.llm.generate_voice.assert_awaited_once()
        self.assertNotIn("自由文本", self.sent_text())
        self.assertEqual(self.client.send.await_count, 2)

    async def test_voice_budget_already_used_cannot_generate_another_card_audio(self):
        self.prepare()
        async def controller(system, prompt, tools, execute, **kwargs):
            await execute("send_voice", {"text": "hello"})
            with self.assertRaisesRegex(WorkspaceError, "单独请求"):
                await execute("submit_word_card", sample_card())
            return "请单独请求解读。"
        self.llm.complete_with_tools.side_effect = controller
        await self.process(wechat_tests.message("读 hello 再解读 unhappy"))
        self.llm.generate_voice.assert_awaited_once()

    async def test_invalid_submission_has_no_side_effect_and_can_be_corrected(self):
        self.prepare()
        async def controller(system, prompt, tools, execute, **kwargs):
            with self.assertRaises(WordCardError):
                await execute("submit_word_card", sample_card(meanings=[]))
            self.client.send.assert_not_awaited()
            self.llm.generate_voice.assert_not_awaited()
            return (await execute("submit_word_card", sample_card())).text
        self.llm.complete_with_tools.side_effect = controller
        await self.process(wechat_tests.message("解读 unhappy"))
        self.llm.generate_voice.assert_awaited_once()

    async def test_synthesis_failure_keeps_card_and_adds_one_failure_notice(self):
        self.prepare()
        self.llm.generate_voice.side_effect = LLMError("upstream unavailable")
        await self.process(wechat_tests.message("解读 unhappy"))
        self.assertEqual(self.client.send.await_count, 2)
        self.assertIn("构词拆解", self.sent_text())
        self.assertIn("读音生成失败", self.sent_text())
        self.transport.media.upload.assert_not_awaited()

    async def test_text_send_unknown_stops_before_synthesis_and_does_not_retry(self):
        self.prepare()
        self.client.send.side_effect = DeliveryUnknown("unknown")
        await self.process(wechat_tests.message("解读 unhappy"))
        self.llm.generate_voice.assert_not_awaited()
        self.client.send.assert_awaited_once()

    async def test_voice_send_unknown_does_not_repeat_card_or_add_notice(self):
        self.prepare()
        self.client.send.side_effect = [{"id": "text"}, DeliveryUnknown("unknown")]
        await self.process(wechat_tests.message("解读 unhappy"))
        self.llm.generate_voice.assert_awaited_once()
        self.assertEqual(self.client.send.await_count, 2)
        self.assertTrue(self.transport.uncertain)

    async def test_qq_queue_does_not_send_failure_notice_after_unconfirmed_delivery(self):
        self.prepare()
        for fail_text in (True, False):
            with self.subTest(fail_text=fail_text):
                transport = SimpleNamespace(
                    send_text=AsyncMock(side_effect=TransportError("delivery unknown") if fail_text else None),
                    send_file=AsyncMock(side_effect=TransportError("delivery unknown")))
                self.bot._channel_transport = transport
                self.bot._qq = SimpleNamespace(get_me=AsyncMock(return_value={"id": "bot"}))
                self.llm.generate_voice.reset_mock()
                await self.bot.start()
                try:
                    await self.bot.handle_message(IncomingMessage("qq", "qq-group", "unknown-" + str(fail_text),
                        "owner", "老师", "owner", "解读 unhappy", "解读 unhappy"))
                    await asyncio.wait_for(self.bot._task_ready_groups.join(), 3)
                finally:
                    await self.bot.close()
                transport.send_text.assert_awaited_once()
                self.assertEqual(self.llm.generate_voice.await_count, 0 if fail_text else 1)
                row = self.store._db.execute("SELECT status, terminal_sent FROM task_runs WHERE message_id=?",
                    ("unknown-" + str(fail_text),)).fetchone()
                self.assertEqual(tuple(row), ("failed", 0))

    async def test_history_failure_after_card_send_does_not_retry_or_start_voice(self):
        self.prepare()
        add = self.store.add
        async def fail_bot_history(*args, **kwargs):
            if kwargs.get("is_bot"):
                raise OSError("history unavailable after delivery")
            return await add(*args, **kwargs)
        self.store.add = fail_bot_history
        await self.process(wechat_tests.message("解读 unhappy"))
        self.client.send.assert_awaited_once()
        self.llm.generate_voice.assert_not_awaited()

    async def test_revoked_memory_after_submission_sends_no_card_or_voice(self):
        self.prepare()
        memory = self.memory_guard()
        async def controller(system, prompt, tools, execute, **kwargs):
            result = await execute("submit_word_card", sample_card())
            memory.valid = lambda: False
            return result.text
        self.llm.complete_with_tools.side_effect = controller
        await self.process(wechat_tests.message("解读 unhappy"))
        self.assertNotIn("构词拆解", self.sent_text())
        self.llm.generate_voice.assert_not_awaited()

    async def test_revocation_during_synthesis_prevents_audio_upload(self):
        self.prepare()
        memory = self.memory_guard()
        async def synthesize(*args, **kwargs):
            memory.valid = lambda: False
            return b"audio"
        self.llm.generate_voice.side_effect = synthesize
        await self.process(wechat_tests.message("解读 unhappy"))
        self.llm.generate_voice.assert_awaited_once()
        self.transport.media.upload.assert_not_awaited()
        # The durable WeChat worker may add its existing generic failure notice.
        self.assertEqual(self.sent_text().count("构词拆解"), 1)
        self.assertTrue(all(call.args[2]["type"] == 1 for call in self.client.send.await_args_list))

    async def test_card_still_redacts_internal_values(self):
        self.prepare(card=sample_card(formation="内部资料 " + self.settings.llm_api_key, pronounce=False))
        await self.process(wechat_tests.message("解读 unhappy"))
        self.assertNotIn(self.settings.llm_api_key, self.sent_text())
        self.assertIn("[内部信息已隐藏]", self.sent_text())


if __name__ == "__main__":
    unittest.main()
