from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.bot import PersonaBot
from app.channel import IncomingMessage, TransportError
from app.config import Settings
from app.memory.__main__ import restore
from app.memory.identity import Actor, Identities, digest
from app.memory.service import MemoryService, MODEL
from app.memory.store import SharedMemory, MemoryDenied, MemoryUnavailable, TZ, packed
from app.memory.transport import MemoryTransport
from app.storage import MemoryStore
from app.wechat.chat import ChatRuntime
from app.wechat.client import SessionExpired, ProtocolError
from app.wechat.inbox import Inbox
from app.wechat.state import State

CONFIG = {'qq': {'account':'qq-bot','owner':'qq-owner'},
          'wechat': {'account':'wx-bot','owner':'wx-owner'}, 'person':'teacher'}


def message(channel='qq', user='qq-owner', group='group-a', text='我的海棠项目周五上线', identity='1', **kw):
    return IncomingMessage(channel, group, identity, user, '老师', 'owner', text, text, **kw)


class MemoryFixture:
    def setup_memory(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 22, 3, 59, tzinfo=TZ).timestamp()
        self.identities = Identities(CONFIG)
        self.shared = SharedMemory(self.root/'shared', clock=lambda:self.now)
        self.qq = self.identities.actor(message(), 'qq-bot')
        self.wx = self.identities.actor(message('wechat','wx-owner','private'), 'wx-bot')
        self.other = self.identities.actor(message(group='group-b'), 'qq-bot')

    def event(self, text='我的海棠项目周五上线', identity='1', actor=None):
        actor = actor or self.qq
        return dict(id=identity, text=text, channel=actor.channel, subject=actor.person,
                    scope=actor.scope, owner=actor.owner, ns=int(self.now*1e9))

    def add(self, text='我的海棠项目周五上线', identity='1', actor=None, topic='海棠项目进度', **kw):
        event = self.event(text, identity, actor)
        count = self.shared.ingest(event, [dict(content=text, evidence=text, topic=topic, self=True, **kw)])
        return digest(identity, text)[:24], count

    def cleanup_memory(self):
        self.shared.close()
        self.temp.cleanup()


class MemoryStoreTests(MemoryFixture, unittest.TestCase):
    def setUp(self): self.setup_memory()
    def tearDown(self): self.cleanup_memory()

    def test_owner_qq_facts_reach_private_but_not_other_group(self):
        identity, _ = self.add()
        self.assertEqual(self.shared.visible(self.wx, '海棠项目进展')[0]['id'], identity)
        self.assertEqual(self.shared.visible(self.other), [])

    def test_startup_during_offline_restore_fails_without_blocking_chat(self):
        self.shared.close()
        with open(self.root/'shared/runtime.lock','a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(MemoryUnavailable): SharedMemory(self.root/'shared')
        self.shared=SharedMemory(self.root/'shared',clock=lambda:self.now)

    def test_nickname_and_role_cannot_bind_identity(self):
        impostor = self.identities.actor(message(user='stranger'), 'qq-bot')
        self.assertFalse(impostor.owner)
        self.assertNotEqual(impostor.person, self.wx.person)
        self.add(actor=impostor)
        self.assertEqual(self.shared.visible(self.wx), [])
        with self.assertRaises(ValueError):
            self.identities.actor(message('wechat','stranger','private'), 'wx-bot')
        with self.assertRaises(ValueError):
            self.identities.actor(message(), 'another-bot')

    def test_private_default_and_explicit_share_unshare(self):
        identity, _ = self.add('我喜欢无糖咖啡', actor=self.wx)
        self.assertEqual(self.shared.visible(self.qq), [])
        self.shared.revise(self.wx, identity, 'share', self.qq.scope)
        rows = self.shared.visible(self.qq)
        self.assertEqual(len(rows), 1)
        with self.assertRaises(MemoryDenied):
            self.shared.revise(self.qq, identity, 'edit', '冒充修改')
        refs = {identity:rows[0]['revision']}
        self.shared.revise(self.wx, identity, 'unshare', self.qq.scope)
        self.assertFalse(self.shared.validate(self.qq, refs))
        self.assertEqual(self.shared.visible(self.qq), [])

    def test_group_member_cannot_edit_another_member_or_authorize_group(self):
        stranger = self.identities.actor(replace(message(user='member'), role='member'), 'qq-bot')
        identity, _ = self.add()
        with self.assertRaises(MemoryDenied):
            self.shared.revise(stranger, identity, 'forget')
        with self.assertRaises(MemoryDenied):
            self.shared.revise(self.qq, identity, 'share', self.other.scope)

    def test_non_first_person_does_not_leave_owner_group(self):
        self.add('小王喜欢红茶')
        self.assertEqual(self.shared.visible(self.wx), [])

    def test_event_dedup_and_exact_evidence_validation(self):
        self.assertEqual(self.add()[1], 1)
        self.assertEqual(self.add()[1], 0)
        self.assertEqual(self.shared.ingest(self.event(identity='bad'),
            [dict(content='虚构', evidence='不存在的原文', topic='进度')]), 0)

    def test_conflicts_preserve_both_sources(self):
        first, _ = self.add()
        self.now += 1
        second, _ = self.add('我的海棠项目下周上线', identity='2')
        rows = self.shared.visible(self.wx, '海棠')
        self.assertEqual({row['id'] for row in rows}, {first, second})
        self.assertTrue(all(row['conflict'] for row in rows))
        self.assertFalse(self.shared.validate(self.wx, {first:1}))

    def test_correction_cancels_old_pending_extraction_and_summary(self):
        identity, _ = self.add()
        pending = self.event('我的海棠项目周五上线', 'pending')
        batch = self.shared.batches()[0]
        self.now += 1
        self.shared.revise(self.wx, identity, 'edit', '我的海棠项目下周上线')
        self.assertFalse(self.shared.publish(batch, '旧摘要'))
        self.assertEqual(self.shared.ingest(pending, [dict(content=pending['text'],
            evidence=pending['text'], topic='海棠项目进度', self=True)]), 0)
        self.assertEqual(self.shared.visible(self.wx)[0]['revision'], 2)
        self.assertFalse(self.shared.source_valid('1'))

    def test_forget_invalidates_published_summary_immediately(self):
        identity, _ = self.add()
        self.shared.publish(self.shared.batches()[0], '摘要')
        self.shared.revise(self.wx, identity, 'forget')
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM summaries').fetchone()[0], 0)
        self.assertEqual(self.shared.visible(self.wx), [])

    def test_shared_budgets_are_atomic_across_connections(self):
        second = SharedMemory(self.root/'shared', clock=lambda:self.now)
        try:
            for i in range(100):
                self.assertTrue((second if i%2 else self.shared).reserve('extract', 100))
            self.assertFalse(second.reserve('extract', 100))
            self.now += 86400
            self.assertTrue(second.reserve('extract', 100))
        finally:
            second.close()

    def test_journal_replays_crash_after_durable_append_before_db_commit(self):
        identity, _ = self.add()
        original = self.shared._sync
        calls = 0
        def crash():
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError('simulated disk failure')
            return original()
        with patch.object(self.shared, '_sync', side_effect=crash):
            with self.assertRaises(OSError): self.shared.revise(self.wx, identity, 'forget')
        self.assertEqual(self.shared.visible(self.wx), [])

    def test_restore_replays_delete_edit_and_unshare(self):
        deleted, _ = self.add(identity='d')
        edited, _ = self.add('我喜欢绿茶', identity='e', topic='饮品')
        private, _ = self.add('我的私人项目代号青鸟', identity='p', actor=self.wx, topic='私事')
        self.shared.revise(self.wx, private, 'share', self.qq.scope)
        backup = self.shared.backup()
        self.now += 1
        self.shared.revise(self.wx, deleted, 'forget')
        self.shared.revise(self.wx, edited, 'edit', '我喜欢红茶')
        self.shared.revise(self.wx, private, 'unshare', self.qq.scope)
        with self.assertRaises(MemoryUnavailable): restore(self.root/'shared', backup)
        self.shared.close()
        restore(self.root/'shared', backup)
        self.shared = SharedMemory(self.root/'shared', clock=lambda:self.now)
        wx = {row['id']:row for row in self.shared.visible(self.wx)}
        self.assertNotIn(deleted, wx)
        self.assertEqual(wx[edited]['content'], '我喜欢红茶')
        self.assertNotIn(private, {row['id'] for row in self.shared.visible(self.qq)})

    def test_truncated_or_missing_journal_blocks_reads_and_restore(self):
        identity, _ = self.add()
        backup = self.shared.backup()
        self.shared.revise(self.wx, identity, 'forget')
        lines = self.shared.log.read_text().splitlines(True)
        self.shared.log.write_text(''.join(lines[:-1]))
        with self.assertRaises(MemoryUnavailable): self.shared.visible(self.wx)
        self.shared.close()
        with self.assertRaises(MemoryUnavailable): restore(self.root/'shared', backup)
        self.shared.log.write_text(''.join(lines))
        self.shared = SharedMemory(self.root/'shared', clock=lambda:self.now)

    def test_compression_partitions_exact_acl_and_bounded_batches(self):
        for i in range(70):
            self.add('我的项目'+str(i)+'测试'+('详细信息'*70), str(i), topic=str(i))
        self.add('我的私人安排周日休息', 'private', actor=self.wx)
        batches = self.shared.batches()
        self.assertGreater(len(batches), 2)
        for batch in batches:
            self.assertLessEqual(len(batch), 50)
            self.assertEqual(len({row['acl'] for row in batch}), 1)
            payload = [{k:row[k] for k in ('id','content','evidence','topic','conflict')} for row in batch]
            self.assertLessEqual(len(packed(payload)), 12000)

    def test_chinese_retrieval_caps_and_time_order(self):
        for i in range(12):
            self.now += 1
            self.add('我的海棠项目第'+str(i)+'个里程碑'+('测试'*200), str(i), topic=str(i))
        rows = self.shared.visible(self.wx, '海棠进展')
        self.assertLessEqual(len(rows), 8)
        self.assertLessEqual(sum(len(self.shared.render(row)) for row in rows), 3000)
        self.assertIn('11', rows[0]['content'])
        self.assertEqual(self.shared.visible(self.wx,'不存在的旅行'), [])

    def test_backup_retention_and_active_facts_survive_cleanup(self):
        self.add()
        for _ in range(16):
            self.shared.backup()
            self.now += 86400
        self.assertEqual(len(list((self.root/'shared/backups').glob('*.db'))), 14)
        self.assertEqual(len(self.shared.visible(self.wx)), 1)

    def test_retired_revision_text_removed_but_revocation_replay_survives(self):
        identity,_ = self.add()
        self.shared.revise(self.wx, identity, 'edit', '旧纠正文本不可永久保存')
        self.now += 1
        self.shared.revise(self.wx, identity, 'edit', '当前纠正文本需要保留')
        self.now += 31*86400
        self.shared.backup()
        self.assertNotIn('旧纠正文本', self.shared.log.read_text())
        self.assertIn('当前纠正文本', self.shared.log.read_text())
        self.assertEqual(self.shared.visible(self.wx)[0]['content'], '当前纠正文本需要保留')
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM versions').fetchone()[0],0)


class MemoryServiceTests(MemoryFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_memory()
        self.runtime = MemoryStore(str(self.root/'runtime.db'))
        self.llm = SimpleNamespace(complete=AsyncMock(return_value='{"facts":[]}'))
        self.service = MemoryService(self.root/'shared', self.identities, 'qq-bot', self.runtime,
            self.llm, maintainer=True, clock=lambda:self.now)

    async def asyncTearDown(self):
        await self.service.close()
        self.runtime.close()
        self.cleanup_memory()

    async def queue(self, incoming=None):
        incoming = incoming or message()
        with self.service.turn(incoming):
            await self.runtime.add(incoming.conversation_id, incoming.user_id, incoming.username, incoming.content)
            self.assertIsNone(self.runtime.memory_pending(self.now))

    async def test_new_only_durable_queue_failure_retry_and_no_tools(self):
        await self.runtime.add('old', 'old', 'old', '旧聊天不得导入')
        await self.queue()
        self.llm.complete.side_effect = RuntimeError('provider failure')
        self.assertFalse(await self.service.extract_one())
        self.assertEqual(self.runtime.memory_status()['pending'], 1)
        self.now += 31
        self.llm.complete.side_effect = None
        self.llm.complete.return_value = packed({'facts':[dict(content='我的海棠项目周五上线',
            evidence='我的海棠项目周五上线', topic='海棠项目', self=True)]})
        self.assertTrue(await self.service.extract_one())
        self.assertEqual(self.runtime.memory_status()['pending'], 0)
        self.assertEqual(len(self.shared.visible(self.wx)), 1)
        self.assertEqual(self.llm.complete.call_args.kwargs, {'model': MODEL})
        await self.queue()  # Duplicate runtime message does not trigger extraction twice.
        self.assertFalse(await self.service.extract_one())

    async def test_budget_defers_without_discarding(self):
        await self.queue()
        for _ in range(100): self.shared.reserve('extract',100)
        self.assertFalse(await self.service.extract_one())
        self.llm.complete.assert_not_called()
        self.assertEqual(self.runtime.memory_status()['pending'],1)

    async def test_compound_project_keeps_subject_evidence_for_cross_channel_recall(self):
        text = '我的跨端测试项目叫海棠，下一步是整理交接文档。'
        await self.queue(message(text=text))
        # The extractor may split facts, but each needs its original subject context.
        self.llm.complete.return_value = packed({'facts':[
            dict(content='发言者的跨端测试项目叫海棠。', evidence=text,
                 topic='海棠项目名称', self=True),
            dict(content='发言者的海棠项目下一步是整理交接文档。', evidence=text,
                 topic='海棠项目进展', self=True)]})
        self.assertTrue(await self.service.extract_one())
        recalled, refs = self.shared.recall(self.wx, '我刚在 QQ 提到的海棠项目，下一步是什么？')
        self.assertIn('整理交接文档', recalled)
        self.assertEqual(len(refs), 2)
        self.assertEqual(self.shared.visible(self.other, '海棠'), [])

    async def test_truncated_subject_evidence_still_cannot_expand_permissions(self):
        text = '我的跨端测试项目叫海棠，下一步是整理交接文档。'
        await self.queue(message(text=text))
        self.llm.complete.return_value = packed({'facts':[
            dict(content='发言者的项目下一步是整理交接文档。', evidence='下一步是整理交接文档',
                 topic='项目进展', self=True)]})
        self.assertTrue(await self.service.extract_one())
        self.assertEqual(len(self.shared.visible(self.qq)), 1)
        self.assertEqual(self.shared.visible(self.wx), [])

    async def test_crash_after_shared_commit_reuses_event_without_second_model_call(self):
        await self.queue()
        identity,event,_ = self.runtime.memory_pending(self.now)
        self.shared.ingest(event, [dict(content=event['text'], evidence=event['text'], topic='海棠', self=True)])
        self.assertTrue(await self.service.extract_one())
        self.llm.complete.assert_not_called()
        self.assertEqual(self.runtime.memory_status()['pending'],0)

    async def test_non_participation_commands_emotes_not_queued(self):
        for i, incoming in enumerate([message(text='/reset'), message(text='收到'),
                message(text='😀😀😀😀😀'), message(should_answer=False)]):
            await self.queue(replace(incoming,message_id=str(i)))
        self.assertEqual(self.runtime.memory_status()['pending'],0)

    async def test_history_and_generated_file_dependencies_invalidate(self):
        identity, _ = self.add()
        with self.service.turn(message()):
            self.assertIn('海棠', self.service.prompt('海棠'))
            await self.runtime.add('group-a','bot','夏莉','旧的海棠记忆',is_bot=True)
            self.service.file_refs('group-a','generated.txt',write=True)
        self.shared.revise(self.wx,identity,'forget')
        with self.service.turn(message(identity='2')):
            self.assertEqual(await self.runtime.history('group-a',20),[])
            self.assertFalse(self.service.file_refs('group-a','generated.txt'))

    async def test_edit_command_text_cannot_reappear_from_short_term_history(self):
        identity,_=self.add()
        self.now += 1
        command='/memory edit '+identity+' 我的私人代号不应从旧命令复活'
        with self.service.turn(message(text=command,identity='edit-command')):
            await self.runtime.add('group-a','qq-owner','老师',command)
            self.assertIn('已纠正',self.service.command(command))
        self.shared.revise(self.wx,identity,'forget')
        with self.service.turn(message(identity='next')):
            self.assertEqual(await self.runtime.history('group-a',20),[])

    async def test_transport_rechecks_stale_reply_and_file(self):
        identity,_ = self.add()
        transport = SimpleNamespace(send_text=AsyncMock(),send_file=AsyncMock())
        guarded = MemoryTransport(transport,self.service)
        with self.service.turn(message()):
            self.service.prompt('海棠')
            self.shared.revise(self.wx,identity,'forget')
            with self.assertRaises(TransportError): await guarded.send_file('group-a',Path('out.txt'),'1')
            await guarded.send_text('group-a','旧事实不得发出','1')
        self.assertNotIn('旧事实',transport.send_text.call_args.args[1])
        transport.send_file.assert_not_called()

    async def test_scheduler_catchup_four_am_no_changes_and_single_maintainer(self):
        self.llm.complete.return_value='有效摘要'
        self.add()
        await self.service.maintenance()  # One catch-up slot before today's 04:00.
        self.assertEqual(self.llm.complete.call_count,1)
        self.now += 120
        await self.service.maintenance()
        self.assertEqual(self.llm.complete.call_count,1)  # No changed facts: zero calls.
        self.now += 5*86400
        self.add('我的新计划明天开始','new')
        await self.service.maintenance()
        self.assertEqual(self.llm.complete.call_count,2)  # No day-by-day catch-up.
        await self.service.maintenance()
        self.assertEqual(self.llm.complete.call_count,2)
        self.service.maintainer=False
        self.now += 86400
        await self.service.maintenance()
        self.assertEqual(self.llm.complete.call_count,2)

    async def test_parallel_compaction_correction_rejects_stale_publication(self):
        identity,_ = self.add()
        async def generate(*args,**kw):
            with self.assertRaises(MemoryUnavailable):
                with self.shared.lock('maintenance.lock'): pass
            self.shared.revise(self.wx,identity,'forget')
            return '含旧事实的摘要'
        self.llm.complete.side_effect=generate
        await self.service.maintenance()
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM summaries').fetchone()[0],0)
        self.assertEqual(self.shared.visible(self.wx),[])

    async def test_daily_compression_budget_and_retry_failure(self):
        for i in range(6): self.add('我的计划'+str(i),str(i),actor=replace(self.qq,scope='group:'+str(i)))
        self.llm.complete.return_value='整理结果'
        await self.service.maintenance()
        self.assertEqual(self.llm.complete.call_count,4)
        self.assertEqual(len(self.shared.batches()),2)
        self.now += 86400
        self.llm.complete.side_effect=RuntimeError('upstream')
        with self.assertRaises(RuntimeError): await self.service.maintenance()
        self.assertEqual(len(self.shared.batches()),2)
        self.assertEqual(len(self.shared.visible(self.wx)),6)

    async def test_memory_unavailable_preserves_chat_and_never_claims_write(self):
        self.shared.log.rename(self.shared.log.with_suffix('.saved'))
        with self.service.turn(message()):
            await self.runtime.add('group-a','qq-owner','老师','普通聊天')
            self.assertIn('暂不可用',self.service.prompt('海棠'))
            self.assertIn('未成功',self.service.command('/memory remember 我喜欢红茶'))
        self.assertEqual(self.runtime.memory_status()['pending'],1)

    async def test_reset_keeps_long_term_facts(self):
        self.add()
        await self.runtime.reset_group('group-a')
        self.assertEqual(len(self.shared.visible(self.qq)),1)

    async def test_actual_bot_prompt_filters_private_memory_and_refuses_stale_generation(self):
        env={'QQ_APP_ID':'qq-bot','QQ_APP_SECRET':'fake','LLM_BASE_URL':'https://example.test',
             'LLM_API_KEY':'fake','LLM_MODEL':'Fusion/qqchatbot','EMOTE_ENABLED':'false','WEB_TOOLS_ENABLED':'false'}
        with patch.dict(os.environ,env,clear=True): settings=Settings.from_env()
        persona=self.root/'persona.md'
        persona.write_text('你是夏莉。')
        settings=replace(settings,workspace_root=str(self.root/'files'),persona_path=str(persona),
                         owner_user_ids=frozenset({'qq-owner'}),agent_access='admins')
        private,_ = self.add('我的秘密代号蓝鲸',identity='private',actor=self.wx)
        public,_ = self.add()
        async def generate(system,prompt,*args,**kwargs):
            self.assertNotIn('蓝鲸',prompt)
            self.assertIn('海棠',prompt)
            self.shared.revise(self.wx,public,'forget')
            return '海棠项目周五上线'
        llm=SimpleNamespace(complete=AsyncMock(side_effect=generate),complete_with_tools=AsyncMock(side_effect=generate))
        transport=SimpleNamespace(send_text=AsyncMock(return_value={}),send_file=AsyncMock())
        bot=PersonaBot(settings,None,llm,self.runtime,transport=transport,memory=self.service)
        await bot.handle_message(message(text='我的海棠项目进展如何',identity='bot-test'),_from_task_queue=True)
        self.assertTrue(transport.send_text.called)
        self.assertNotIn('周五上线',transport.send_text.call_args.args[1])


class WeChatResidentTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_login_waits_without_poll_or_restart_loop(self):
        client=SimpleNamespace(updates=AsyncMock(side_effect=SessionExpired('expired')))
        runtime=ChatRuntime(client,SimpleNamespace(meta=lambda k:''),SimpleNamespace(),SimpleNamespace(),asyncio.Event())
        task=asyncio.create_task(runtime.receive())
        await asyncio.sleep(.01)
        self.assertFalse(runtime.login_ready)
        self.assertFalse(task.done())
        self.assertEqual(client.updates.call_count,1)
        runtime.stop.set()
        await task

    async def test_migrated_state_refuses_any_second_poller(self):
        with tempfile.TemporaryDirectory() as directory:
            state=State(Path(directory))
            state.write('migrated.json',{'moved':True})
            with self.assertRaises(ProtocolError):
                with state.lock(): pass

    async def test_shutdown_drains_current_handler(self):
        entered, finish=asyncio.Event(),asyncio.Event()
        async def work():
            entered.set()
            await finish.wait()
        runtime=ChatRuntime(SimpleNamespace(),SimpleNamespace(recover=lambda:None),None,None,asyncio.Event())
        runtime.work=work
        runtime.receive=AsyncMock(side_effect=lambda: asyncio.sleep(100))
        runtime.maintain=AsyncMock(side_effect=lambda: asyncio.sleep(100))
        # Real coroutine functions are needed, not AsyncMock returning unawaited awaitables.
        async def idle(): await asyncio.sleep(100)
        runtime.receive=runtime.maintain=idle
        task=asyncio.create_task(runtime.run())
        await entered.wait()
        runtime.stop.set()
        await asyncio.sleep(.01)
        self.assertFalse(task.done())
        finish.set()
        await task
