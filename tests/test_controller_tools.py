"""Exercise the actual controller callback boundary with isolated channel adapters."""
import io
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from app.channel import IncomingMessage, TransportError
from app.llm import LLMError
from app.wechat.client import DeliveryUnknown, text_item
from app.workspace import WorkspaceError
import test_wechat_chat as wechat_tests

message = wechat_tests.message


class ControllerToolsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wechat_tests.WeChatCoreTests.asyncSetUp
    asyncTearDown = wechat_tests.WeChatCoreTests.asyncTearDown
    process = wechat_tests.WeChatCoreTests.process
    sent_text = wechat_tests.WeChatCoreTests.sent_text
    use_tools = wechat_tests.WeChatCoreTests.use_tools
    fake_drawing = wechat_tests.WeChatCoreTests.fake_drawing

    async def test_repeated_image_and_send_file_are_idempotent(self):
        self.fake_drawing()
        async def controller(system, prompt, tools, execute, **kwargs):
            first = await execute('generate_image', {'prompt':'一只白猫'})
            self.assertEqual(first, await execute('generate_image', {'reference':'none','prompt':' 一只白猫 '}))
            result = json.loads(first)
            self.assertEqual(result['status'], 'sent')
            self.assertIn('已发送', await execute('send_file', {'path':result['path']}))
            with self.assertRaisesRegex(WorkspaceError, '调用次数'):
                await execute('generate_image', {'prompt':'另一只猫'})
            return '图片已发。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('画一只白猫'))
        self.llm.generate_image.assert_awaited_once()
        self.transport.media.upload.assert_awaited_once()

    async def test_image_failure_is_returned_and_not_claimed_sent(self):
        self.fake_drawing()
        self.llm.generate_image.side_effect = LLMError('upstream unavailable')
        async def controller(system, prompt, tools, execute, **kwargs):
            for _ in range(2):
                with self.assertRaisesRegex(WorkspaceError, '未完成'):
                    await execute('generate_image', {'prompt':'猫咪'})
            return '这次没画成。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('画图'))
        self.llm.generate_image.assert_awaited_once()
        self.transport.media.upload.assert_not_awaited()
        self.assertIn('没画成', self.sent_text())

    async def test_missing_reference_never_falls_back_to_unreferenced_generation(self):
        self.fake_drawing()
        async def controller(system, prompt, tools, execute, **kwargs):
            with self.assertRaisesRegex(WorkspaceError, '没有可用的参考图片'):
                await execute('generate_image', {'prompt':'改为水彩', 'reference':'current_and_self'})
            return '需要参考图。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('用刚才的图改成水彩'))
        self.llm.generate_image.assert_not_awaited()
        self.llm.edit_image.assert_not_awaited()

    async def test_vision_edit_and_voice_can_be_composed(self):
        data = self.fake_drawing()
        self.bot._settings = replace(self.bot._settings, voice_enabled=True)
        self.transport.media.download = AsyncMock(return_value=(data,'reference.png',2))
        self.llm.complete.return_value = '图片是红色。'
        self.llm.generate_voice = AsyncMock(return_value=b'validated-wav')
        self.use_tools(('understand_images',{'question':'是什么颜色'}),
                       ('generate_image',{'prompt':'把红色背景改成蓝色','reference':'current'}),
                       ('send_voice',{'text':'背景改为蓝色了。'}))
        await self.process(message(item_list=[text_item('看这张图，改成蓝色，用语音说明'),
                                             {'type':2,'image_item':{}}]))
        self.llm.complete.assert_awaited_once()
        self.llm.edit_image.assert_awaited_once()
        self.assertEqual(self.llm.edit_image.await_args.kwargs['images'][0].data, data)
        self.llm.generate_image.assert_not_awaited()
        self.llm.generate_voice.assert_awaited_once()
        self.assertEqual(self.transport.media.upload.await_count, 2)

    async def test_model_cannot_select_arbitrary_file_url_or_other_conversation(self):
        self.fake_drawing()
        bad = [
            ('generate_image', {'prompt':'猫','reference':'/etc/passwd'}),
            ('generate_image', {'prompt':'猫','images':['https://private.invalid/image']}),
            ('understand_images', {'question':'描述','path':'../private.png'}),
            ('recent_requests', {'conversation_id':'other'}),
            ('generate_image', {'prompt':None}),
            ('generate_image', {}),
        ]
        async def controller(system, prompt, tools, execute, **kwargs):
            for name, args in bad:
                with self.assertRaises(WorkspaceError):
                    await execute(name,args)
            return '已拒绝无效参数。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('test'))
        self.llm.generate_image.assert_not_awaited()
        self.llm.complete.assert_not_awaited()

    async def test_disabled_and_unknown_tools_cannot_be_called_by_model(self):
        self.bot._settings = replace(self.settings, image_generation_enabled=False, voice_enabled=False,
                                     vision_enabled=False)
        async def controller(system, prompt, tools, execute, **kwargs):
            for name in ('generate_image','send_voice','understand_images','run_shell'):
                with self.assertRaisesRegex(WorkspaceError,'未启用或当前身份无权'):
                    await execute(name,{'prompt':'test'})
            return '未启用。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('test'))

    async def test_member_cannot_force_media_or_workspace_execution(self):
        self.fake_drawing()
        async def controller(system, prompt, tools, execute, **kwargs):
            for name in ('generate_image','send_voice','write_file','send_file'):
                self.assertNotIn(name,{tool['name'] for tool in tools})
                with self.assertRaisesRegex(WorkspaceError,'无权'):
                    await execute(name,{'prompt':'test'})
            return '没有权限。'
        self.llm.complete_with_tools.side_effect = controller
        payload = message('画图')
        self.transport.bind(payload)
        msg, _ = self.transport.normalize('老师')
        msg = replace(msg, role='member', user_id='impostor', username='老师')
        await self.bot.handle_message(msg, _from_task_queue=True)
        self.llm.generate_image.assert_not_awaited()
        self.llm.complete_with_tools.assert_awaited_once()

    async def test_unknown_media_send_stops_later_tools_and_final_text(self):
        self.fake_drawing()
        self.bot._settings = replace(self.bot._settings, voice_enabled=True)
        self.llm.generate_voice = AsyncMock(return_value=b'audio')
        self.client.send.side_effect = DeliveryUnknown('unknown')
        async def controller(system, prompt, tools, execute, **kwargs):
            with self.assertRaises(WorkspaceError):
                await execute('generate_image', {'prompt':'猫咪'})
            with self.assertRaisesRegex(WorkspaceError,'发送状态尚未确认'):
                await execute('send_voice', {'text':'辛苦了'})
            return '不应发送这段话。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('画图并说话'))
        self.client.send.assert_awaited_once()
        self.llm.generate_voice.assert_not_awaited()
        self.assertTrue(self.transport.uncertain)

    async def test_full_workspace_rejects_before_billed_generation(self):
        self.fake_drawing()
        self.bot._workspace.remaining_bytes = lambda _: 0
        async def controller(system, prompt, tools, execute, **kwargs):
            with self.assertRaisesRegex(WorkspaceError, '配额已用完'):
                await execute('generate_image', {'prompt':'猫咪'})
            return '空间不足。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('画图'))
        self.llm.generate_image.assert_not_awaited()

    async def test_vision_uses_only_this_conversations_user_image(self):
        data = self.fake_drawing()
        foreign = 'another-conversation'
        path = self.bot._workspace.resolve(foreign, 'other.png', must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        await self.store.add(foreign,'other','用户','图片',attachments=[('other.png','image/png')])
        async def controller(system, prompt, tools, execute, **kwargs):
            with self.assertRaisesRegex(WorkspaceError, '没有找到可用图片'):
                await execute('understand_images', {'question':'描述图片'})
            return '本会话没有图片。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('看图片'))
        self.llm.complete.assert_not_awaited()

    async def test_web_search_is_controller_selected_and_bounded(self):
        research = SimpleNamespace(as_prompt=lambda: '真实来源')
        self.bot._web_tools = SimpleNamespace(research=AsyncMock(return_value=research))
        await self.process(message('柚子社最新作是什么'))
        self.bot._web_tools.research.assert_not_awaited()
        async def controller(system, prompt, tools, execute, **kwargs):
            for _ in range(5):
                self.assertEqual(await execute('research', {'query':'最新作'}),'真实来源')
            from app.webtools import WebToolError
            with self.assertRaises(WebToolError):
                await execute('research', {'query':'最新作'})
            return '根据来源回复。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('请查证',identity=2))
        self.assertEqual(self.bot._web_tools.research.await_count,5)

    async def test_qq_voice_still_uses_silk_and_shared_callback(self):
        self.bot._settings = replace(self.settings, voice_enabled=True)
        transport = SimpleNamespace(send_file=AsyncMock(return_value={'id':'sent'}),
                                    send_text=AsyncMock(return_value={'id':'sent'}))
        self.bot._channel_transport = transport
        self.llm.generate_voice = AsyncMock(return_value=b'silk')
        self.use_tools(('send_voice', {'text':'老师，辛苦啦'}))
        msg = IncomingMessage(channel='qq', conversation_id='qq-group', message_id='one',
            user_id='owner', username='老师', role='owner', content='用语音回复', command_content='用语音回复')
        await self.bot.handle_message(msg, _from_task_queue=True)
        self.assertEqual(self.llm.generate_voice.await_args.kwargs['audio_format'],'silk')
        transport.send_file.assert_awaited_once()

    async def test_qq_controller_turn_uses_existing_queue_without_guessing_media(self):
        self.bot._task_workers = [object()]
        msg = IncomingMessage(channel='qq', conversation_id='qq-group', message_id='one',
            user_id='owner', username='老师', role='owner', content='再试试昨晚没成功的画图',
            command_content='再试试昨晚没成功的画图')
        await self.bot.handle_message(msg)
        self.assertEqual(self.bot.task_queue_size, 1)
        queued = self.bot._task_groups['qq-group'][0]
        self.assertEqual(queued.intent.value, 'chat')
        self.assertTrue(queued.ready.is_set())
        self.llm.complete_with_tools.assert_not_awaited()
        self.client.send.assert_not_awaited()

    async def test_progress_is_sent_only_after_tool_selection_and_once(self):
        self.fake_drawing()
        self.bot._settings = replace(self.bot._settings, task_progress_ack_enabled=True)
        await self.process(message('你会画画么'))
        self.assertNotIn('正在画图', self.sent_text())
        self.use_tools(('generate_image', {'prompt':'猫咪'}), ('generate_image', {'prompt':'猫咪'}))
        await self.process(message('画一张猫咪',identity=2))
        self.assertEqual(self.sent_text().count('正在画图'),1)
        self.llm.generate_image.assert_awaited_once()

    def memory_guard(self):
        from contextlib import nullcontext
        memory = SimpleNamespace(turn=lambda _:nullcontext(), prompt=lambda _:'',
            valid=lambda: True, file_refs=lambda *args, **kwargs: True, safe_reply=lambda text:text)
        self.bot._memory = memory
        return memory

    async def test_revoked_memory_blocks_media_before_execution(self):
        self.fake_drawing()
        memory = self.memory_guard()
        async def controller(system, prompt, tools, execute, **kwargs):
            memory.valid = lambda: False
            with self.assertRaisesRegex(WorkspaceError,'长期记忆已失效'):
                await execute('generate_image', {'prompt':'猫咪'})
            return '记忆已更新。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('画图'))
        self.llm.generate_image.assert_not_awaited()

    async def test_revocation_during_generation_blocks_image_send(self):
        self.fake_drawing()
        memory = self.memory_guard()
        async def generate(*args, **kwargs):
            memory.valid = lambda: False
            return 'https://dashscope-test.aliyuncs.com/result.png'
        self.llm.generate_image.side_effect = generate
        async def controller(system, prompt, tools, execute, **kwargs):
            with self.assertRaisesRegex(WorkspaceError,'记忆已失效'):
                await execute('generate_image', {'prompt':'猫咪'})
            return '记忆已更新。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('画图'))
        self.llm.generate_image.assert_awaited_once()
        self.transport.media.upload.assert_not_awaited()

    async def test_oversized_voice_argument_does_not_call_paid_api(self):
        self.bot._settings = replace(self.settings, voice_enabled=True, voice_max_chars=10)
        self.llm.generate_voice = AsyncMock()
        async def controller(system, prompt, tools, execute, **kwargs):
            with self.assertRaisesRegex(WorkspaceError,'超长'):
                await execute('send_voice', {'text':'测'*11})
            return '需要缩短。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('语音回复'))
        self.llm.generate_voice.assert_not_awaited()

    async def test_word_repeat_selects_learning_mode_and_rejects_sentence(self):
        self.bot._settings = replace(self.settings, voice_enabled=True)
        self.llm.generate_voice = AsyncMock(return_value=b"audio")
        self.transport.media.upload = AsyncMock(return_value={"type":4,"file_item":{}})
        async def controller(system, prompt, tools, execute, **kwargs):
            with self.assertRaises(WorkspaceError):
                await execute("send_voice", {"text":"read this sentence", "mode":"word"})
            with self.assertRaises(WorkspaceError):
                await execute("send_voice", {"text":"unhappy", "mode":"invalid"})
            await execute("send_voice", {"text":"unhappy", "mode":"word"})
            return "读给你听了。"
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message("再读一下 unhappy"))
        self.llm.generate_voice.assert_awaited_once()
        self.assertEqual(self.llm.generate_voice.await_args.kwargs["mode"], "word")

    async def test_disabled_edit_is_not_advertised_and_invalid_selection_does_not_use_budget(self):
        self.fake_drawing()
        self.bot._settings = replace(self.bot._settings, image_edit_enabled=False)
        async def controller(system, prompt, tools, execute, **kwargs):
            tool = next(tool for tool in tools if tool['name']=='generate_image')
            self.assertEqual(tool['input_schema']['properties']['reference']['enum'], ['none'])
            with self.assertRaisesRegex(WorkspaceError,'不支持的参考'):
                await execute('generate_image', {'prompt':'猫咪','reference':'self'})
            self.assertEqual(json.loads(await execute('generate_image', {'prompt':'猫咪'}))['status'],'sent')
            return '已发出。'
        self.llm.complete_with_tools.side_effect = controller
        await self.process(message('画个猫咪'))
        self.llm.generate_image.assert_awaited_once()
