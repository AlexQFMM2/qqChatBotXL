"""Keep configured model limits and both channel controller prompts aligned."""
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from app.channel import IncomingMessage
from app.config import Settings
from app.llm import FinalToolReply, LLMClient, LLMOutputLimitError
import test_wechat_chat as wechat_tests


class ReplyBudgetTests(TestCase):
    def test_default_and_overrides_have_one_configured_source(self):
        with patch.dict(os.environ, wechat_tests.ENV, clear=True):
            settings = Settings.from_env(channel="wechat")
            self.assertEqual((settings.llm_max_tokens, settings.max_reply_chars), (2000, 1500))
            with patch.dict(os.environ, {"LLM_MAX_TOKENS": "1750", "MAX_REPLY_CHARS": "950"}):
                settings = Settings.from_env(channel="wechat")
                self.assertEqual((settings.llm_max_tokens, settings.max_reply_chars), (1750, 950))


class ControllerBudgetTests(IsolatedAsyncioTestCase):
    asyncSetUp = wechat_tests.WeChatCoreTests.asyncSetUp
    asyncTearDown = wechat_tests.WeChatCoreTests.asyncTearDown

    async def test_both_channels_and_protocols_receive_actual_limits_with_and_without_tools(self):
        self.bot._channel_transport = SimpleNamespace(send_text=AsyncMock(return_value={"id": "sent"}))
        for channel in ("qq", "wechat"):
            for api_format in ("anthropic", "openai"):
                for use_tools in (False, True):
                    with self.subTest(channel=channel, api_format=api_format, tools=use_tools):
                        # Nondefault values prove the prompt isn't frozen at 2000/1500.
                        settings = replace(self.bot._settings, llm_max_tokens=1750, max_reply_chars=950,
                                           llm_api_format=api_format)
                        self.bot._settings = settings
                        client = LLMClient(settings, None)
                        if api_format == "anthropic":
                            response = {"content": [{"type": "text", "text": "老师，重点已经讲完整啦。"}], "stop_reason": "end_turn"}
                        else:
                            response = {"choices": [{"message": {"content": "老师，重点已经讲完整啦。"}, "finish_reason": "stop"}]}
                        client._post = AsyncMock(return_value=response)
                        self.bot._llm = client
                        tools = self.bot._tools_for_request(role="owner", user_id="owner", task_intent=None)
                        identity = f"{channel}-{api_format}-{use_tools}"
                        msg = IncomingMessage(channel, identity, identity, "owner", "老师", "owner", "讲讲词源", "讲讲词源")
                        with patch.object(self.bot, "_tools_for_request", return_value=tools if use_tools else []):
                            await self.bot.handle_message(msg, _from_task_queue=True)
                        client._post.assert_awaited_once()
                        payload = client._post.await_args.args[2]
                        system = payload["system"] if api_format == "anthropic" else payload["messages"][0]["content"]
                        self.assertEqual(payload["max_tokens"], 1750)
                        self.assertIn("1750 tokens", system)
                        self.assertIn("最多 950 个字符", system)
                        self.assertIn("完整结尾", system)
                        self.assertIn("保留全部固定栏目", system)


class BudgetRecoveryTests(IsolatedAsyncioTestCase):
    def client(self, protocol):
        return LLMClient(SimpleNamespace(llm_api_format=protocol, llm_base_url="https://example.test",
            llm_api_key="fake", llm_model="model", llm_max_tokens=2000, llm_temperature=0.8), None)

    def response(self, protocol, *, limited=False, tool=None, text="完整回答。"):
        if protocol == "anthropic":
            content = ([{"type": "tool_use", "id": "tool-1", "name": "work", "input": {"value": tool}}]
                       if tool else [{"type": "text", "text": text}])
            return {"content": content, "stop_reason": "max_tokens" if limited else "tool_use" if tool else "end_turn"}
        message = ({"tool_calls": [{"id": "tool-1", "type": "function", "function": {
            "name": "work", "arguments": '{"value":"' + tool + '"}'}}]} if tool else {"content": text})
        return {"choices": [{"message": message, "finish_reason": "length" if limited else "tool_calls" if tool else "stop"}]}

    @staticmethod
    def tools():
        return [{"name": "work", "description": "work", "input_schema": {"type": "object"}}]

    async def test_clipped_text_is_replaced_with_one_complete_answer_within_same_budget(self):
        for protocol in ("anthropic", "openai"):
            for tools in ([], self.tools()):
                with self.subTest(protocol=protocol, tools=bool(tools)):
                    client = self.client(protocol)
                    client._post = AsyncMock(side_effect=[self.response(protocol, limited=True, text="没写完的"), self.response(protocol)])
                    execute = AsyncMock()
                    result = await client.complete_with_tools("system", "user", tools, execute)
                    self.assertEqual(result, "完整回答。")
                    self.assertEqual(client._post.await_count, 2)
                    for call in client._post.await_args_list:
                        self.assertEqual(call.args[2]["max_tokens"], 2000)
                    self.assertNotIn("tools", client._post.await_args.args[2])
                    execute.assert_not_awaited()

    async def test_truncated_tool_response_is_not_executed_even_with_parseable_arguments(self):
        for protocol in ("anthropic", "openai"):
            with self.subTest(protocol=protocol):
                client = self.client(protocol)
                client._post = AsyncMock(side_effect=[self.response(protocol, limited=True, tool="partial"),
                                                      self.response(protocol, tool="complete")])
                execute = AsyncMock(return_value=FinalToolReply("完整卡片。"))
                self.assertEqual(await client.complete_with_tools("system", "user", self.tools(), execute), "完整卡片。")
                execute.assert_awaited_once_with("work", {"value": "complete"})

    async def test_rewriting_final_answer_preserves_completed_tools_without_replaying(self):
        for protocol in ("anthropic", "openai"):
            with self.subTest(protocol=protocol):
                client = self.client(protocol)
                client._post = AsyncMock(side_effect=[self.response(protocol, tool="complete"),
                    self.response(protocol, limited=True, text="结果还没说"), self.response(protocol)])
                execute = AsyncMock(return_value="已完成")
                self.assertEqual(await client.complete_with_tools("system", "user", self.tools(), execute), "完整回答。")
                execute.assert_awaited_once()
                final_payload = client._post.await_args.args[2]
                self.assertNotIn("tools", final_payload)
                self.assertIn("已完成", str(final_payload["messages"]))

    async def test_second_truncation_stops_without_unbounded_retry_or_tool_execution(self):
        for protocol in ("anthropic", "openai"):
            with self.subTest(protocol=protocol):
                client = self.client(protocol)
                client._post = AsyncMock(return_value=self.response(protocol, limited=True, tool="partial"))
                execute = AsyncMock()
                with self.assertRaises(LLMOutputLimitError):
                    await client.complete_with_tools("system", "user", self.tools(), execute)
                self.assertEqual(client._post.await_count, 2)
                execute.assert_not_awaited()
