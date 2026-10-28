from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook
from PIL import Image

from app.bot import PersonaBot
from app.channel import TransportError
from app.config import Settings
from app.domain import ChatLine, build_user_prompt
from app.wechat.chat import ChatRuntime, chat_settings, load_environment, failure_details
from app.llm import LLMError
from app.wechat.client import DeliveryUnknown, ProtocolError, text_item
from app.wechat.inbox import Inbox, InboxFull
from app.wechat.state import State
from app.wechat.transport import WeChatTransport, conversation_id
from app.workspace import WorkspaceError

ACCOUNT = {'token': 'fake-token', 'bot_id': 'bot', 'owner': 'trusted-owner',
           'base_url': 'https://ilinkai.weixin.qq.com'}
ENV = {'LLM_BASE_URL': 'https://example.test', 'LLM_API_KEY': 'fake-model-key',
       'LLM_MODEL': 'Fusion/qqchatbot', 'EMOTE_ENABLED': 'false', 'VISION_ROUTE': 'fusion', 'TASK_PROGRESS_ACK_ENABLED':'false'}


def message(text='你好', identity=1, **overrides):
    return {'message_id': identity, 'from_user_id': ACCOUNT['owner'], 'to_user_id': ACCOUNT['bot_id'],
            'message_type': 1, 'message_state': 2, 'context_token': 'private-context',
            'create_time_ms': int(time.time()*1000), 'item_list': [text_item(text)], **overrides}


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'chat.db'
        self.store = Inbox(self.path, ACCOUNT, since_ms=0)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_cursor_and_entire_batch_rollback_on_backpressure(self):
        batch = {'msgs':[message(identity=1),message(identity=2)],'get_updates_buf':'new'}
        with self.assertRaises(InboxFull): self.store.accept(batch, limit=1)
        self.assertEqual(self.store.meta('cursor'), '')
        self.assertIsNone(self.store.next())
        self.assertEqual(self.store.accept(batch), 2)
        self.assertEqual(self.store.meta('cursor'), 'new')

    def test_duplicate_64bit_id_and_terminal_payload_redaction(self):
        batch={'msgs':[message(identity=9223372036854775807)],'get_updates_buf':'v1'}
        self.assertEqual(self.store.accept(batch),1)
        self.assertEqual(self.store.accept(batch),0)
        self.assertEqual(self.store.next()[0], '9223372036854775807')
        self.store.mark('9223372036854775807','done')
        payload=self.store._db.execute('SELECT payload FROM wx_inbox').fetchone()[0]
        self.assertEqual(payload,'{}')
        self.assertEqual(self.store.accept(batch),0)

    def test_admission_does_not_trust_names_groups_receivers_or_bot_messages(self):
        msgs=[message(identity=i+1,**change) for i,change in enumerate([
            {'from_user_id':'stranger'}, {'to_user_id':'another-bot'}, {'group_id':'group'},
            {'message_type':2}, {'message_state':1}, {'context_token':''},
            {'message_id':True}, {'create_time_ms':None},
        ])]
        self.assertEqual(self.store.accept({'msgs':msgs}),0)

    def test_restart_preserves_queued_and_interrupts_started_work(self):
        self.store.accept({'msgs':[message(identity=1),message(identity=2)]})
        self.store.mark('1','running')
        self.store.begin_send('out','1')
        self.store.close()
        self.store=Inbox(self.path,ACCOUNT,since_ms=int(time.time()*1000)+10000)
        self.store.recover()
        self.assertEqual(self.store.next()[2],'interrupted')
        self.assertEqual(self.store.begin_send('out','1')[0],'unknown')
        self.store.mark('1','failed')
        self.assertEqual(self.store.next()[0],'2')

    def test_wrong_account_cannot_reuse_runtime(self):
        with self.assertRaises(ProtocolError):
            Inbox(self.path,{**ACCOUNT,'owner':'someone-else'})

    def test_first_activation_skips_historical_probe_messages(self):
        self.store.close()
        self.path.unlink()
        self.store=Inbox(self.path,ACCOUNT)
        self.assertEqual(self.store.accept({'msgs':[message(create_time_ms=1)]}),0)

    def test_pruned_ids_do_not_reactivate_old_messages(self):
        old=message(create_time_ms=1)
        self.store.accept({'msgs':[old]})
        self.store.mark('1','done')
        self.store._db.execute('UPDATE wx_inbox SET updated_at=1')
        self.store._db.commit()
        self.store.prune()
        self.assertEqual(self.store.accept({'msgs':[old]}),0)


class WeChatCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.state=State(Path(self.temp.name)/'private')
        with patch.dict(os.environ, ENV, clear=True):
            self.settings=chat_settings(self.state,ACCOUNT)
        self.store=Inbox(self.state.directory/'chat.db',ACCOUNT,since_ms=0)
        self.client=SimpleNamespace(send=AsyncMock(return_value={'message_id':99}))
        self.transport=WeChatTransport(self.client,self.store,ACCOUNT)
        self.llm=SimpleNamespace(complete=AsyncMock(return_value='老师，我在。'),
                                complete_with_tools=AsyncMock(return_value='老师，我在。'))
        self.bot=PersonaBot(self.settings,None,self.llm,self.store,transport=self.transport)
        self.runtime=ChatRuntime(self.client,self.store,self.transport,self.bot,asyncio.Event())

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def process(self,payload):
        self.store.accept({'msgs':[payload]})
        await self.runtime.process_one()

    def sent_text(self):
        return '\n'.join(c.args[2].get('text_item',{}).get('text','') for c in self.client.send.await_args_list)

    async def test_plain_text_uses_existing_persona_model_and_private_history(self):
        await self.process(message('你好夏莉'))
        self.llm.complete_with_tools.assert_awaited_once()
        system,prompt=self.llm.complete_with_tools.await_args.args[:2]
        self.assertIn('2933',system)
        self.assertIn('微信私聊',prompt)
        self.assertNotIn('下面是这个群',prompt)
        self.assertIn('扫码身份已验证',prompt)
        self.assertEqual(self.sent_text(),'老师，我在。')
        history=await self.store.history(self.transport.conversation_id,10)
        self.assertEqual(len(history),2)
        self.assertEqual(await self.store.history('qq-other-group',10),[])

    async def test_duplicate_input_only_calls_model_once(self):
        await self.process(message())
        await self.process(message())
        self.llm.complete_with_tools.assert_awaited_once()
        self.client.send.assert_awaited_once()

    async def test_builtin_emote_uses_wechat_image_send_and_hides_internal_marker(self):
        with patch.dict(os.environ,{**ENV,'EMOTE_ENABLED':'true'},clear=True):
            settings=chat_settings(self.state,ACCOUNT)
        self.assertTrue(settings.emote_enabled)
        self.bot=PersonaBot(settings,None,self.llm,self.store,transport=self.transport)
        self.runtime.bot=self.bot
        self.llm.complete_with_tools.return_value='老师，谢谢你。[[EMOTE:害羞]]'
        self.transport.media.upload=AsyncMock(return_value={'type':2,'image_item':{}})
        await self.process(message('夏莉，你真可爱'))
        self.assertEqual(self.sent_text().strip(),'老师，谢谢你。')
        self.assertEqual([call.args[2]['type'] for call in self.client.send.await_args_list],[1,2])
        uploaded=self.transport.media.upload.await_args.args
        self.assertEqual(uploaded[:3],((Path(settings.emote_path)/'害羞.png').read_bytes(),'害羞.png',2))
        history=await self.store.history(self.transport.conversation_id,10)
        self.assertTrue(any(line.content=='[发送表情：害羞]' for line in history))

        # Even if the API echoes our text/image, neither becomes a new user turn.
        echoes=[message(identity=100+i,from_user_id=ACCOUNT['bot_id'],
                        to_user_id=ACCOUNT['owner'],message_type=2,item_list=[call.args[2]])
                for i,call in enumerate(self.client.send.await_args_list)]
        echoes.append({**echoes[-1],'message_id':102,'message_type':1})
        echoes.append({**message(identity=103),'message_type':2})
        self.assertEqual(self.store.accept({'msgs':echoes}),0)
        self.assertFalse(await self.runtime.process_one())
        self.assertEqual(self.llm.complete_with_tools.await_count,1)
        self.assertEqual(self.client.send.await_count,2)

        # The bot's emote history must not become a user's recent uploaded image.
        self.llm.complete_with_tools.return_value='还没有收到老师上传的图片。'
        await self.process(message('再看刚才那张图片',identity=2))
        self.assertEqual(self.llm.complete_with_tools.await_args.kwargs['images'],[])

        self.bot._settings=replace(settings,emote_enabled=False)
        self.llm.complete_with_tools.return_value='老师，谢谢你。[[EMOTE:害羞]]'
        self.client.send.reset_mock()
        self.transport.media.upload.reset_mock()
        await self.process(message('夏莉，你真可爱',identity=3))
        self.assertEqual(self.sent_text(),'老师，谢谢你。')
        self.transport.media.upload.assert_not_awaited()

    async def test_user_forwarded_emote_is_a_single_explicit_image_request(self):
        await self.bot._seed_builtin_media()
        original=(Path(self.settings.emote_path)/'害羞.png').read_bytes()
        self.transport.media.download=AsyncMock(return_value=(original,'forwarded.png',2))
        forwarded=message(item_list=[text_item('看看这张图片'),{'type':2,'image_item':{}}])
        self.use_tools(('understand_images', {'question':'描述图片'}))
        await self.process(forwarded)
        images=self.llm.complete.await_args.kwargs['images']
        self.assertEqual([image.data for image in images],[original])
        await self.process(forwarded)
        self.assertFalse(await self.runtime.process_one())
        self.llm.complete_with_tools.assert_awaited_once()
        self.client.send.assert_awaited_once()

    async def test_file_is_saved_then_read_by_existing_workspace_tool(self):
        item={'type':4,'file_item':{'file_name':'notes.txt','media':{}}}
        self.transport.media.download=AsyncMock(return_value=(b'hello-file','notes.txt',4))
        async def complete(system,prompt,tools,execute,**kwargs):
            files=await execute('list_files',{})
            self.assertIn('inbox/notes.txt',files)
            self.assertEqual(await execute('read_file',{'path':'inbox/notes.txt'}),'hello-file')
            return '文件内容是 hello-file。'
        self.llm.complete_with_tools.side_effect=complete
        await self.process(message(item_list=[text_item('读取文件'),item]))
        self.assertIn('hello-file',self.sent_text())
        path=self.bot._workspace.resolve(self.transport.conversation_id,'inbox/notes.txt')
        self.assertEqual(path.read_bytes(),b'hello-file')

    async def test_xlsx_uses_existing_workbook_reader(self):
        book=Workbook(); book.active.append(['名称','数量']); book.active.append(['苹果',3])
        buf=io.BytesIO(); book.save(buf); book.close()
        self.transport.media.download=AsyncMock(return_value=(buf.getvalue(),'table.xlsx',4))
        await self.process(message(item_list=[{'type':4,'file_item':{'file_name':'table.xlsx'}}]))
        await self.process(message('/read inbox/table.xlsx',identity=2))
        self.assertIn('苹果',self.sent_text())
        self.assertIn('3',self.sent_text())

    async def test_file_generation_and_send_use_same_tools(self):
        self.transport.media.upload=AsyncMock(return_value={'type':4,'file_item':{}})
        async def complete(system,prompt,tools,execute,**kwargs):
            await execute('write_file',{'path':'out.txt','content':'common-tool-output'})
            await execute('send_file',{'path':'out.txt'})
            return '文件已发出。'
        self.llm.complete_with_tools.side_effect=complete
        await self.process(message('帮我写一个文本文件'))
        self.assertEqual(self.transport.media.upload.await_args.args[0],b'common-tool-output')
        self.assertIn('文件已发出',self.sent_text())

    def use_tools(self, *calls, reply='完成。'):
        async def complete(system, prompt, tools, execute, **kwargs):
            self.assertEqual(kwargs['images'], [])  # The controller gets tools, not pre-read images.
            for name, args in calls:
                self.assertIn(name, {tool['name'] for tool in tools})
                await execute(name, args)
            return reply
        self.llm.complete_with_tools.side_effect = complete

    def fake_drawing(self):
        self.bot._settings=replace(self.bot._settings,image_generation_enabled=True,
            image_edit_enabled=True,image_text_overlay_enabled=False)
        self.llm.generate_image=AsyncMock(return_value='https://dashscope-test.aliyuncs.com/result.png')
        self.llm.edit_image=AsyncMock(return_value='https://dashscope-test.aliyuncs.com/result.png')
        buf=io.BytesIO();Image.new('RGB',(4,4),'red').save(buf,format='PNG')
        async def download(reference,target,maximum):target.write_bytes(buf.getvalue())
        self.transport.download_attachment=AsyncMock(side_effect=download)
        self.transport.media.upload=AsyncMock(return_value={'type':2,'image_item':{}})
        return buf.getvalue()

    async def test_image_is_read_only_when_controller_chooses_vision(self):
        buf=io.BytesIO(); Image.new('RGB',(4,4),'red').save(buf,format='PNG')
        self.transport.media.download=AsyncMock(return_value=(buf.getvalue(),'sample.png',2))
        self.use_tools(('understand_images',{'question':'是什么颜色'}))
        await self.process(message(item_list=[text_item('是什么颜色'),{'type':2,'image_item':{}}]))
        self.assertTrue(self.llm.complete.await_args.kwargs['images'])
        self.llm.complete.reset_mock()
        self.llm.complete_with_tools.side_effect=None
        await self.process(message('今天心情很好',identity=2))
        self.llm.complete.assert_not_awaited()

    async def test_colloquial_drawing_calls_controller_then_generator_and_sends_image(self):
        self.fake_drawing()
        self.use_tools(('generate_image',{'prompt':'温暖治愈的水彩猫咪'}))
        await self.process(message('那画个图安慰安慰上了一天班的我吧'))
        self.llm.generate_image.assert_awaited_once()
        self.llm.complete_with_tools.assert_awaited_once()
        self.assertEqual(self.transport.media.upload.await_args.args[2],2)
        self.assertEqual(self.store._db.execute('SELECT status FROM wx_inbox').fetchone()[0],'done')

    async def test_explicit_voice_uses_controller_and_wav_in_wechat(self):
        self.bot._settings=replace(self.settings,voice_enabled=True,voice_max_mb=5)
        self.llm.generate_voice=AsyncMock(return_value=b'validated-wav-data')
        self.transport.media.upload=AsyncMock(return_value={'type':4,'file_item':{}})
        self.use_tools(('send_voice',{'text':'老师，辛苦啦'}))
        await self.process(message('/voice 老师，辛苦啦'))
        self.assertEqual(self.llm.generate_voice.await_args.kwargs['audio_format'],'wav')
        upload=self.transport.media.upload.await_args
        self.assertTrue(upload.args[1].endswith('.wav'))
        self.assertEqual(upload.args[2],4)

    async def test_retry_can_recall_only_current_users_requests_in_this_conversation(self):
        self.fake_drawing()
        group=self.transport.conversation_id
        await self.store.add(group,ACCOUNT['owner'],'老师','画一幅治愈的猫咪图')
        await self.store.add(group,'bot','夏莉','画一张机器人想象',is_bot=True)
        await self.store.add(group,'other-user','访客','画别人的私人照片')
        await self.store.add('other-conversation',ACCOUNT['owner'],'老师','画其他会话的图')
        async def complete(system,prompt,tools,execute,**kwargs):
            past=await execute('recent_requests',{})
            self.assertIn('治愈的猫咪图',past)
            for private in ('机器人想象','私人照片','其他会话的图'):
                self.assertNotIn(private,past)
            await execute('generate_image',{'prompt':'一幅治愈的猫咪图'})
            return '图片已发出。'
        self.llm.complete_with_tools.side_effect=complete
        for identity,text in enumerate(('昨晚没画成的图，再试试看','再试试昨晚没成功的画图','那张图再来一次'),start=1):
            await self.process(message(text,identity=identity))
            self.assertEqual(self.llm.generate_image.await_count,identity)

    async def test_no_media_side_effect_without_controller_tool_call(self):
        self.fake_drawing()
        for identity,text in enumerate(('画一张猫咪','别再画昨晚的图了','能画图吗','/image 猫咪'),start=1):
            await self.process(message(text,identity=identity))
        self.llm.generate_image.assert_not_awaited()
        self.transport.media.upload.assert_not_awaited()
        self.assertEqual(self.llm.complete_with_tools.await_count,4)

    async def test_voice_failure_is_returned_to_controller_and_not_reexecuted(self):
        self.bot._settings=replace(self.settings,voice_enabled=True)
        self.llm.generate_voice=AsyncMock(side_effect=LLMError('safe failure'))
        self.transport.media.upload=AsyncMock()
        async def complete(system,prompt,tools,execute,**kwargs):
            for _ in range(2):
                with self.assertRaisesRegex(WorkspaceError,'未完成'):
                    await execute('send_voice',{'text':'老师辛苦了'})
            return '语音生成失败，先用文字回复。'
        self.llm.complete_with_tools.side_effect=complete
        await self.process(message('请用语音回复我'))
        self.assertIn('语音生成失败',self.sent_text())
        self.llm.generate_voice.assert_awaited_once()
        self.transport.media.upload.assert_not_awaited()

    async def test_followup_phrase_reuses_saved_image_after_failed_first_attempt(self):
        buf=io.BytesIO(); Image.new('RGB',(4,4),'red').save(buf,format='PNG')
        original=buf.getvalue()
        self.transport.media.download=AsyncMock(return_value=(original,'sample.png',2))
        self.llm.complete_with_tools.side_effect=LLMError('模型接口 HTTP 502: upstream failure')
        await self.process(message(item_list=[{'type':2,'image_item':{}}]))
        self.use_tools(('understand_images',{'question':'再看刚才的图片'}))
        await self.process(message('再看刚才那张图片',identity=2))
        self.assertEqual([im.data for im in self.llm.complete.await_args.kwargs['images']],[original])
        self.transport.media.download.assert_awaited_once()

    async def test_explicit_followup_without_saved_image_cannot_invent_one(self):
        async def complete(system,prompt,tools,execute,**kwargs):
            with self.assertRaisesRegex(WorkspaceError,'没有找到可用图片'):
                await execute('understand_images',{'question':'看图片'})
            return '没有找到图片。'
        self.llm.complete_with_tools.side_effect=complete
        await self.process(message('再看刚才那张图片'))
        self.llm.complete.assert_not_awaited()
        self.assertIn('没有找到图片',self.sent_text())

    async def test_photo_followup_reset_cannot_reuse_old_image(self):
        buf=io.BytesIO(); Image.new('RGB',(4,4),'red').save(buf,format='PNG')
        self.transport.media.download=AsyncMock(return_value=(buf.getvalue(),'photo.png',2))
        self.use_tools(('understand_images',{'question':'图片是什么颜色'}))
        await self.process(message(item_list=[{'type':2,'image_item':{}}]))
        await self.process(message('看看那张照片是什么颜色',identity=2))
        self.assertEqual(self.llm.complete.await_count,2)
        await self.process(message('/reset',identity=3))
        async def complete(system,prompt,tools,execute,**kwargs):
            with self.assertRaisesRegex(WorkspaceError,'没有找到可用图片'):
                await execute('understand_images',{'question':'看旧图'})
            return '没有找到图片。'
        self.llm.complete_with_tools.side_effect=complete
        await self.process(message('再看刚才那张图片',identity=4))
        self.assertEqual(self.llm.complete.await_count,2)

    async def test_attachment_download_failure_is_not_reported_as_seen(self):
        self.transport.media.download=AsyncMock(side_effect=ProtocolError('safe-failure'))
        await self.process(message(item_list=[{'type':4,'file_item':{'file_name':'file.txt'}}]))
        self.assertIn('部分附件保存失败',self.llm.complete_with_tools.await_args.args[1])

    async def test_reset_clears_only_private_history_and_keeps_files(self):
        group=self.transport.conversation_id
        await self.store.add('qq-other','a','a','preserve')
        self.bot._workspace.write_text(group,'keep.txt','preserve')
        await self.process(message())
        await self.process(message('/reset',identity=2))
        history=await self.store.history(group,10)
        self.assertEqual(len(history),1)  # Reset confirmation only.
        self.assertEqual(len(await self.store.history('qq-other',10)),1)
        self.assertEqual(self.bot._workspace.read_text(group,'keep.txt'),'preserve')

    async def test_unsupported_input_gives_feedback_without_calling_model(self):
        await self.process(message(item_list=[{'type':3,'voice_item':{}}]))
        self.llm.complete_with_tools.assert_not_awaited()
        self.assertIn('暂不支持',self.sent_text())

    async def test_unknown_send_stops_followup_and_is_not_replayed(self):
        self.client.send.side_effect=DeliveryUnknown('safe-error')
        await self.process(message())
        self.assertEqual(self.client.send.await_count,1)
        self.assertEqual(self.store._db.execute('SELECT status FROM wx_inbox').fetchone()[0],'unknown')
        await self.process(message())
        self.assertEqual(self.client.send.await_count,1)

    async def test_send_success_with_journal_failure_prevents_followup(self):
        self.transport.bind(message())
        with patch.object(self.store,'end_send',side_effect=OSError('disk full')):
            with self.assertRaises(TransportError):
                await self.transport.send_text(self.transport.conversation_id,'hello','1')
        with self.assertRaises(TransportError):
            await self.transport.send_text(self.transport.conversation_id,'retry','1')
        self.client.send.assert_awaited_once()
        self.store.recover()
        self.assertEqual(self.store._db.execute('SELECT status FROM wx_outbox').fetchone()[0],'unknown')

    async def test_unknown_file_send_blocks_later_tool_side_effects(self):
        self.client.send.side_effect=DeliveryUnknown('safe-error')
        self.transport.media.upload=AsyncMock(return_value={'type':4,'file_item':{}})
        async def complete(system,prompt,tools,execute,**kwargs):
            await execute('write_file',{'path':'keep.txt','content':'first'})
            with self.assertRaises(TransportError):
                await execute('send_file',{'path':'keep.txt'})
            with self.assertRaises(WorkspaceError):
                await execute('write_file',{'path':'keep.txt','content':'second'})
            return '请核对手机。'
        self.llm.complete_with_tools.side_effect=complete
        await self.process(message('写文件'))
        self.assertEqual(self.bot._workspace.read_text(self.transport.conversation_id,'keep.txt'),'first')
        self.assertEqual(self.store._db.execute('SELECT status FROM wx_inbox').fetchone()[0],'unknown')
        await self.process(message())
        self.assertEqual(self.client.send.await_count,1)

    async def test_interrupted_work_notifies_without_repeating_tools(self):
        p=message()
        self.store.accept({'msgs':[p]})
        self.transport.bind(p)
        await self.transport.send_text(self.transport.conversation_id,'earlier progress','1')
        self.store.mark('1','running')
        self.store.recover()
        await self.runtime.process_one()
        self.llm.complete_with_tools.assert_not_awaited()
        self.assertIn('服务重启',self.sent_text())
        self.assertEqual(self.client.send.await_count,2)

    async def test_chunking_stays_within_utf8_limit(self):
        self.transport.bind(message())
        text='中文'*3000
        await self.transport.send_text(self.transport.conversation_id,text,'1')
        self.assertEqual(self.sent_text().replace('\n',''),text)
        for call in self.client.send.await_args_list:
            self.assertLessEqual(len(call.args[2]['text_item']['text'].encode()),3900)

    async def test_wrong_recipient_and_missing_context_are_rejected(self):
        self.transport.bind(message())
        with self.assertRaises(TransportError):
            await self.transport.send_text('qq-other','hello','1')
        with self.assertRaises(TransportError):
            self.transport.bind(message(from_user_id='stranger'))
        self.client.send.assert_not_awaited()

    async def test_model_failure_is_sanitized(self):
        self.llm.complete_with_tools.side_effect=RuntimeError('secret-provider-key private-context')
        await self.process(message())
        self.assertNotIn('secret-provider',self.sent_text())
        self.assertNotIn('private-context',self.sent_text())


class EntrySettingsTests(unittest.TestCase):
    def test_wechat_voice_opt_in_is_file_based_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            state=State(Path(directory))
            with patch.dict(os.environ,{**ENV,'VOICE_ENABLED':'true'},clear=True):
                self.assertFalse(chat_settings(state,ACCOUNT).voice_enabled)
            with patch.dict(os.environ,{**ENV,'WECHAT_VOICE_ENABLED':'true','VOICE_MAX_MB':'8'},clear=True):
                settings=chat_settings(state,ACCOUNT)
                self.assertTrue(settings.voice_enabled)
                self.assertEqual(settings.voice_max_mb,5)

    def test_failure_details_expose_status_without_provider_body(self):
        error=RuntimeError('wrapper-with-private-content')
        error.__cause__=LLMError('模型接口 HTTP 400: secret-key private-image-data')
        self.assertEqual(failure_details(error),'LLMError:HTTP_400')
        self.assertEqual(failure_details(LLMError('模型接口请求超时')),'LLMError:timeout')
        self.assertEqual(failure_details(LLMError('模型重试后仍没有返回文本内容')),
                         'LLMError:empty_response')

    def test_wechat_does_not_need_qq_secrets(self):
        with patch.dict(os.environ,ENV,clear=True):
            s=Settings.from_env(channel='wechat')
            self.assertEqual(s.qq_app_secret,'')
            with self.assertRaises(ValueError): Settings.from_env()

    def test_env_files_never_execute_shell_text_and_shell_has_priority(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'LLM_MODEL':'shell'},clear=True):
            path=Path(d)/'model.env'
            path.write_text('LLM_MODEL=file\nSAFE=$(must-not-run)\nJSON={"a":"b"}\n')
            load_environment([path])
            self.assertEqual(os.environ['SAFE'],'$(must-not-run)')
            self.assertEqual(os.environ['LLM_MODEL'],'shell')
            self.assertEqual(json.loads(os.environ['JSON']),{'a':'b'})

    def test_qq_prompt_keeps_existing_default(self):
        prompt=build_user_prompt([ChatLine('friend','hello')])
        self.assertIn('下面是这个群',prompt)


if __name__=='__main__':
    unittest.main()
