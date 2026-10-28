"""Time anchors must survive history retrieval, overnight turns and old-request lookup."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import json

from app.channel import IncomingMessage
from app.domain import ChatLine, build_time_context, build_user_prompt
import test_wechat_chat as wechat_tests


class TimeContextTests(TestCase):
    def test_beijing_new_year_rollover_uses_explicit_timezone(self):
        context = build_time_context(datetime(2026, 12, 31, 16, tzinfo=timezone.utc))
        self.assertIn('2027-01-01 00:00:00，星期五', context)
        self.assertIn('昨天是 2026-12-31', context)
        self.assertIn('明天是 2027-01-02', context)
        self.assertIn('Asia/Shanghai，UTC+08:00', context)

    def test_relative_dates_cover_leap_day(self):
        context = build_time_context(datetime(2028, 3, 1, 7, tzinfo=timezone.utc))
        self.assertIn('2028-03-01 15:00:00，星期三', context)
        self.assertIn('昨天是 2028-02-29', context)
        self.assertIn('明天是 2028-03-02', context)

    def test_undated_legacy_line_is_not_given_todays_date(self):
        prompt = build_user_prompt([ChatLine('老师', '今晚继续')])
        self.assertIn('[记录时间未知] 老师: 今晚继续', prompt)


class ControllerTimeTests(IsolatedAsyncioTestCase):
    asyncSetUp = wechat_tests.WeChatCoreTests.asyncSetUp
    asyncTearDown = wechat_tests.WeChatCoreTests.asyncTearDown

    async def test_both_channels_refresh_clock_and_preserve_overnight_history(self):
        self.bot._channel_transport = SimpleNamespace(send_text=AsyncMock(return_value={'id': 'sent'}))
        # The same running bot handles last night's message and today's reply.
        night = datetime(2026, 10, 22, 15, 55, tzinfo=timezone.utc)
        afternoon = datetime(2026, 10, 23, 7, 5, tzinfo=timezone.utc)
        for channel in ('qq', 'wechat'):
            for use_tools in (False, True):
                with self.subTest(channel=channel, use_tools=use_tools):
                    conversation = f'{channel}-{use_tools}'
                    selected_tools = self.bot._tools_for_request(role='owner', user_id='owner', task_intent=None)
                    with patch.object(self.bot, '_tools_for_request', return_value=selected_tools if use_tools else []):
                        for index, (now, content) in enumerate(((night, '今晚先聊到这里'), (afternoon, '我们继续吧'))):
                            msg = IncomingMessage(channel=channel, conversation_id=conversation,
                                message_id=f'{conversation}-{index}', user_id='owner', username='老师',
                                role='owner', content=content, command_content=content)
                            with patch('app.domain.datetime', wraps=datetime) as clock, \
                                 patch('app.storage.time.time', return_value=now.timestamp()):
                                clock.now.return_value = now
                                await self.bot.handle_message(msg, _from_task_queue=True)
                            call = self.llm.complete_with_tools if use_tools else self.llm.complete
                            system, prompt = call.await_args.args[:2]
                            expected = '2026-10-22 23:55:00' if index == 0 else '2026-10-23 15:05:00'
                            self.assertIn(expected, system)
                            if index:
                                self.assertIn('今天是 2026-10-23；昨天是 2026-10-22', system)
                                self.assertIn('[2026-10-22 23:55:00 +08:00]', prompt)
                                self.assertIn('今晚先聊到这里', prompt)
                                self.assertIn('[2026-10-23 15:05:00 +08:00]', prompt)
                                self.assertIn('我们继续吧', prompt)

    async def test_recent_requests_keep_dates_scope_and_whole_entries_under_limit(self):
        conversation = 'time-test'
        night = datetime(2026, 10, 22, 15, 55, tzinfo=timezone.utc)
        with patch('app.storage.time.time', return_value=night.timestamp()):
            for index in range(8):
                await self.store.add(conversation, 'owner', '老师', f'昨晚的要求{index}：' + '画' * 1900)
            await self.store.add(conversation, 'other', '其他人', '其他人的秘密')
            await self.store.add(conversation, 'bot', '夏莉', '机器人猜测', is_bot=True)
            await self.store.add('other-conversation', 'owner', '老师', '另一个会话的秘密')

        async def controller(system, prompt, tools, execute, **kwargs):
            result = json.loads(await execute('recent_requests', {}))
            messages = result['messages']
            self.assertLessEqual(len(messages), 12000)
            self.assertNotIn('其他人的秘密', messages)
            self.assertNotIn('另一个会话的秘密', messages)
            self.assertNotIn('机器人猜测', messages)
            self.assertNotIn('昨晚的要求0', messages)
            self.assertIn('[2026-10-22 23:55:00 +08:00] 昨晚的要求7', messages)
            self.assertTrue(all(line.startswith('[2026-10-') for line in messages.splitlines()))
            self.assertTrue(messages.endswith('继续之前的要求'))
            return '老师，我们接着聊。'

        self.llm.complete_with_tools.side_effect = controller
        self.bot._channel_transport = SimpleNamespace(send_text=AsyncMock(return_value={'id': 'sent'}))
        msg = IncomingMessage(channel='qq', conversation_id=conversation, message_id='new',
            user_id='owner', username='老师', role='owner', content='继续之前的要求', command_content='继续之前的要求')
        with patch('app.storage.time.time', return_value=night.timestamp() + 86400):
            await self.bot.handle_message(msg, _from_task_queue=True)
        self.llm.complete_with_tools.assert_awaited_once()
