import asyncio
import copy
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web

from app.channel import IncomingMessage, QQTransport, BattleNoticeRejected, TransportError
from app.llm import LLMError
from app.qq import QQClient, QQAPIError
from app.pokemon import PokemonClient, PokemonError, PokemonGames, BattleStore, BUSY, TERMINAL, render_battle, battle_facts, BATTLE_ROLE_PROMPT
from app.storage import MemoryStore
import test_wechat_chat as wx


def incoming(user='A', conversation='group-a', channel='qq', identity='message-1', text='来一场宝可梦对战'):
    return IncomingMessage(channel, conversation, identity, user, user, 'member', text, text)


class FakeCB:
    token = 'isolated-test-token'
    def __init__(self):
        self.instance = 'instance-1'
        self.owner = None
        self.view = None
        self.calls = []
        self.ops = {}
        self.drop_next_post = False
        self.end_on_move = False

    async def request(self, method, path, *, owner='', instance='', data=None, params=None):
        self.calls.append((method,path,copy.deepcopy(data)))
        if path == '/chat-battles/availability':
            active=self.view and self.view['phase'] not in TERMINAL
            return {'instanceId':self.instance, 'status':('self' if self.owner==owner else 'busy') if active else 'free',
                    **({'battle':copy.deepcopy(self.view)} if active and self.owner==owner else {})}
        if path.startswith('/dex/'):
            return {'source':'ChangeBattle','rows':[{'id':'pikachu','nameZh':'皮卡丘'}]}
        if method=='GET':
            if instance != self.instance: raise PokemonError('instance_changed','restart')
            if not self.view or self.owner != owner: raise PokemonError('not_found','missing')
            result=copy.deepcopy(self.view)
            if params: result['operation']={'status':'done' if params['operationId'] in self.ops else 'not_found'}
            return result
        if data['instanceId'] != self.instance: raise PokemonError('instance_changed','restart')
        key=data['operationId']
        if key in self.ops: return copy.deepcopy(self.ops[key])
        if path=='/chat-battles':
            if self.view and self.view['phase'] not in TERMINAL: raise PokemonError('busy',BUSY)
            self.owner=owner
            self.view={'id':data['id'],'instanceId':self.instance,'phase':'rules','revision':1,
                       'expiresAt':int(time.time()*1000)+300000,'turn':0,'winner':None,'summary':'',
                       'candidates':[], 'environment':None,
                       'environments':[{'id':i,'label':label} for i,label in [('standard','普通'),('gen7','Mega / Z招式'),('gen8','极巨化'),('gen9','太晶化')]],
                       'team':[],'actions':[],'events':[]}
        elif data['revision'] != self.view['revision']: raise PokemonError('stale','stale')
        elif path.endswith('/configure'):
            self.view.update(phase='selecting',revision=2,environment={'id':data['environment'],'label':data['environment']},
                candidates=[{'slot':n,'name':'皮卡丘','level':50,'types':['电'],'nature':'胆小','natureEffect':'速度↑10%、攻击↓10%',
                    'item':'电气球','ability':'静电','teraType':'飞行','mechanics':['Z招式'],'moves':['十万伏特']} for n in range(1,7)])
        elif path.endswith('/select'):
            self.view.update(phase='active',revision=3,candidates=[],turn=1,
                             team=[{'slot':1,'name':'皮卡丘','condition':'100/100','active':True}],
                             actions=[{'id':'3-move-1','label':'第1招 十万伏特'}])
        elif path.endswith('/actions'):
            self.view.update(revision=4,turn=2,actions=[{'id':'4-move-1','label':'第1招 十万伏特'}])
            if self.end_on_move: self.finish()
        elif path.endswith('/end'): self.finish()
        self.ops[key]=copy.deepcopy(self.view)
        if self.drop_next_post:
            self.drop_next_post=False
            raise PokemonError('uncertain','response lost')
        return copy.deepcopy(self.view)

    def finish(self, reason='你获胜了！'):
        self.view.update(phase='ended',winner='p1',revision=self.view['revision']+1,summary='宝可梦对战结束：'+reason,actions=[])


class GameStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=MemoryStore(str(Path(self.temp.name)/'chat.db'))
        self.client=FakeCB()
        self.transport=SimpleNamespace(send_text=AsyncMock(return_value={}),send_battle_notice=AsyncMock(return_value={}))
        self.settings=SimpleNamespace(pokemon_battle_enabled=True,pokemon_dex_enabled=True)
        self.games=PokemonGames(self.settings,self.db,self.client,self.transport)
        self.message=incoming()

    async def asyncTearDown(self):
        await self.games.close();self.db.close();self.temp.cleanup()

    async def turn(self, message=None):
        message=message or self.message
        return self.games.turn(message,await self.games.gate(message))

    async def start(self):
        return await (await self.turn()).execute('pokemon_battle',{'action':'start'})

    async def configure(self):
        return await (await self.turn(replace(self.message,message_id='configure'))).execute('pokemon_battle',{'action':'configure','environment':'gen9'})

    async def test_environment_selection_and_full_candidate_details(self):
        v=await self.start()
        self.assertEqual(v['phase'],'rules');self.assertFalse(v['candidates'])
        menu=render_battle(v)
        for label in ('Mega','Z招式','极巨化','太晶化'): self.assertIn(label,menu)
        with self.assertRaises(PokemonError):
            await (await self.turn()).execute('pokemon_battle',{'action':'configure','environment':'invalid'})
        v=await self.configure();text=render_battle(v)
        for info in ('Lv.50','属性：电','胆小','速度↑10%','攻击↓10%','电气球','静电','太晶属性：飞行','特殊机制：Z招式'): self.assertIn(info,text)

    async def test_special_action_needs_explicit_current_message(self):
        await self.start();await self.configure()
        await (await self.turn(incoming(identity='select'))).execute('pokemon_battle',{'action':'select','selection':[1,2,3]})
        for special,text in [('terastallize','太晶化用第一招'),('mega','Mega进化用第一招'),('max','极巨化用第一招'),('zmove','Z招式第一招')]:
            self.client.view['actions']=[{'id':'special-1','label':text,'special':special}]
            count=len(self.client.ops)
            for content in ('第一招','不要'+text):
                with self.assertRaises(PokemonError):
                    await (await self.turn(incoming(identity='reject-'+special+content,text=content))).execute('pokemon_battle',{'action':'act','action_id':'special-1'})
            self.assertEqual(len(self.client.ops),count)
            await (await self.turn(incoming(identity='explicit-'+special,text=text))).execute('pokemon_battle',{'action':'act','action_id':'special-1'})
            self.assertEqual(len(self.client.ops),count+1)

    async def test_narrator_race_and_definite_rejection_reuses_closing(self):
        self.games.narrator=AsyncMock(return_value='下次我可不会轻易认输啦！')
        v=await self.start();self.client.finish();await self.games.refresh(self.games.store.get(v['id']))
        self.transport.send_battle_notice.side_effect=BattleNoticeRejected('no context')
        await asyncio.gather(self.games.notify(v['id']),self.games.notify(v['id']))
        await self.games.refresh(self.games.store.get(v['id']))
        await self.games.notify(v['id'],self.message)
        self.games.narrator.assert_awaited_once()
        self.assertIn('下次我可不会',self.transport.send_text.await_args.args[1])
        self.assertIn('你获胜',self.transport.send_text.await_args.args[1])

    async def test_timeout_narration_failure_keeps_non_result(self):
        self.games.narrator=AsyncMock(side_effect=RuntimeError('offline'))
        v=await self.start();self.client.view.update(phase='expired',winner=None,summary='超时中断，不判胜负。')
        await self.games.refresh(self.games.store.get(v['id']));await self.games.notify(v['id'])
        text=self.transport.send_battle_notice.await_args.args[1]
        self.assertIn('不判胜负',text);self.assertIn('等了你好一会儿',text)
        self.assertNotIn('赢',text);self.assertNotIn('获胜',text)

    async def test_model_facts_have_explicit_owners_and_only_public_events(self):
        view={'id':'private-local-id','phase':'active','turn':2,'team':[{'name':'烈焰猴'}],
              'candidates':[{'name':'水君'}], 'opponent':{'name':'苍响'},
              'events':[{'id':1,'text':'你方使用了近身战'},{'id':2,'text':'我方换上苍响'}],
              'actions':[{'id':'move-1','label':'第一招'}], 'closing':'我以前说错的台词',
              'debug':{'private':'hidden'}}
        facts=battle_facts(view,1)
        self.assertEqual(facts['human_player']['selected_team'][0]['name'],'烈焰猴')
        self.assertEqual(facts['human_player']['candidate_pool'][0]['name'],'水君')
        self.assertEqual(facts['assistant_player']['active_pokemon']['name'],'苍响')
        self.assertEqual(facts['new_public_events'],[{'id':2,'text':'夏莉方换上苍响'}])
        self.assertIn('独立随机三只',facts['team_selection_rule'])
        for ambiguous in ('id','team','opponent','candidates','closing','debug'): self.assertNotIn(ambiguous,facts)

    async def test_candidate_ownership_and_public_roster_survive_updates_and_restart(self):
        await self.start();v=await self.configure()
        await (await self.turn(incoming(identity='pick'))).execute('pokemon_battle',{'action':'select','selection':[1,2,3]})
        self.client.view.update(opponent={'name':'鬃岩狼人'},events=[{'id':1,'text':'我方换上鬃岩狼人'}])
        await self.games.gate(self.message)
        self.client.view.update(opponent={'name':'苍响'},events=[{'id':2,'text':'我方换上苍响'}])
        await self.games.tick()
        row=self.games.store.get(v['id'])
        self.assertEqual(len(row['view']['playerCandidates']),6)
        self.assertEqual(row['view']['assistantRevealed'],['鬃岩狼人','苍响'])
        self.client.finish();await self.games.refresh(row)
        recovered=PokemonGames(self.settings,self.db,self.client,self.transport)
        turn=recovered.turn(self.message,await recovered.gate(self.message))
        prompt=turn.prompt();self.assertIn('仅供复盘',prompt)
        self.assertIn('鬃岩狼人',prompt);self.assertIn('candidate_pool',prompt)
        facts=battle_facts(recovered.store.get(v['id'])['view'])
        self.assertEqual(facts['result']['winner'],'human_player')
        for msg in (incoming('B'),incoming(conversation='other'),incoming(channel='wechat')):
            self.assertNotIn('鬃岩狼人',recovered.turn(msg,await recovered.gate(msg)).prompt())

    async def test_recent_result_never_replaces_new_current_game(self):
        v=await self.start();self.client.finish();await self.games.refresh(self.games.store.get(v['id']))
        msg=incoming(identity='new-game');turn=await self.turn(msg)
        await turn.execute('pokemon_battle',{'action':'start'})
        current=await self.turn(msg)
        self.assertIn('当前宝可梦对局',current.prompt());self.assertNotIn('仅供复盘',current.prompt())

    async def test_cross_channel_busy_and_foreign_scope(self):
        await self.start()
        for message in (incoming('B'),incoming(conversation='another'),incoming(channel='wechat')):
            self.assertEqual((await self.games.gate(message))['status'],'busy')
        self.assertEqual((await self.games.gate(self.message))['status'],'self')
        with self.assertRaises(PokemonError): self.games.owner(incoming(user='unknown'))

    async def test_pending_write_is_persisted_and_recovered_without_second_mutation(self):
        self.client.drop_next_post=True
        with self.assertRaises(PokemonError): await self.start()
        row=self.games.store.all()[0]
        self.assertTrue(row['pending'])
        self.assertEqual(len(self.client.ops),1)
        recovered=PokemonGames(self.settings,self.db,self.client,self.transport)
        await recovered.tick()
        self.assertFalse(recovered.store.get(row['id'])['pending'])
        self.assertEqual(len(self.client.ops),1)
        self.assertEqual(recovered.store.get(row['id'])['view']['phase'],'rules')

    async def test_fixed_turn_precondition_and_one_mutation(self):
        await self.start();await self.configure()
        turn=await self.turn(replace(self.message,message_id='message-2'))
        view=await turn.execute('pokemon_battle',{'action':'select','selection':[3,1,2]})
        self.assertEqual(view['phase'],'active')
        self.assertEqual(await turn.execute('pokemon_battle',{'action':'select','selection':[3,1,2]}),view)
        with self.assertRaises(PokemonError): await turn.execute('pokemon_battle',{'action':'act','action_id':'3-move-1'})
        stale=await self.turn(replace(self.message,message_id='message-3'))
        self.client.view['revision']=9
        await stale.execute('pokemon_battle',{'action':'status'})
        with self.assertRaises(PokemonError): await stale.execute('pokemon_battle',{'action':'act','action_id':'3-move-1'})
        self.assertEqual(self.client.calls[-1][2]['revision'],3)

    async def test_terminal_notice_race_sends_once_to_origin(self):
        v=await self.start();self.client.finish();await self.games.refresh(self.games.store.get(v['id']))
        await asyncio.gather(self.games.notify(v['id']),self.games.notify(v['id'],self.message))
        self.assertEqual(self.transport.send_text.await_count+self.transport.send_battle_notice.await_count,1)
        self.assertEqual(self.games.store.get(v['id'])['notice'],'sent')
        self.assertFalse(await self.games.notify(v['id'],incoming('B')))

    async def test_rejected_notice_waits_for_origin_and_new_game_keeps_old_summary(self):
        v=await self.start();self.client.finish();await self.games.refresh(self.games.store.get(v['id']))
        self.transport.send_battle_notice.side_effect=BattleNoticeRejected('no context')
        await self.games.notify(v['id'])
        self.assertEqual(self.games.store.get(v['id'])['notice'],'failed')
        self.assertEqual((await self.games.gate(incoming('B')))['status'],'free')
        await (await self.turn(incoming('B',identity='new-message'))).execute('pokemon_battle',{'action':'start'})
        self.assertEqual(len(self.games.store.all()),2)
        self.assertFalse(await self.games.pending_notices(incoming('B')))
        self.assertTrue(await self.games.pending_notices(self.message))
        self.transport.send_text.assert_awaited_once()
        self.assertEqual(self.transport.send_text.await_args.args[0],self.message.conversation_id)

    async def test_unknown_delivery_not_retried_and_restart_marks_sending_unknown(self):
        v=await self.start();self.client.finish();await self.games.refresh(self.games.store.get(v['id']))
        self.transport.send_battle_notice.side_effect=TransportError('unknown')
        await self.games.notify(v['id']);await self.games.tick()
        self.transport.send_battle_notice.assert_awaited_once()
        row=self.games.store.get(v['id']);self.assertEqual(row['notice'],'unknown')
        row['notice']='sending';self.games.store.save(row)
        self.assertEqual(BattleStore(self.db).get(v['id'])['notice'],'unknown')

    async def test_cb_restart_interrupts_not_recreates(self):
        v=await self.start();self.client.instance='instance-2'
        await self.games.tick()
        self.assertEqual(self.games.store.get(v['id'])['view']['phase'],'interrupted')
        self.assertEqual(len(self.client.ops),1)
        self.transport.send_battle_notice.assert_awaited_once()

    async def test_schema_rejects_identity_override_bool_selection_and_query_budget(self):
        await self.start()
        for args in ({'action':'act','owner':'elsewhere'}, {'action':'select','selection':[True,2,3]}):
            with self.assertRaises(PokemonError): await (await self.turn()).execute('pokemon_battle',args)
        turn=await self.turn()
        for _ in range(4): await turn.execute('pokemon_dex',{'action':'search','category':'pokemon','query':'皮卡丘'})
        with self.assertRaises(PokemonError): await turn.execute('pokemon_dex',{'action':'search','category':'pokemon'})

    async def test_explicit_status_can_retrieve_retained_unknown_result(self):
        v=await self.start();self.client.finish();await self.games.refresh(self.games.store.get(v['id']))
        row=self.games.store.get(v['id']);row['notice']='unknown';self.games.store.save(row)
        turn=await self.turn(replace(self.message,message_id='query-result'))
        result=await turn.execute('pokemon_battle',{'action':'status'})
        self.assertTrue(turn.is_status);self.assertIn('你获胜',result['summary'])

    async def test_unknown_foreground_notice_stops_further_reply(self):
        v=await self.start();self.client.finish();await self.games.refresh(self.games.store.get(v['id']))
        self.transport.send_text.side_effect=TransportError('ambiguous')
        self.assertIsNone(await self.games.pending_notices(self.message))

    async def test_cancelled_notification_is_never_automatically_replayed(self):
        v=await self.start();self.client.finish();await self.games.refresh(self.games.store.get(v['id']))
        entered=asyncio.Event()
        async def interrupted_send(*args,**kwargs):
            entered.set();await asyncio.Future()
        self.transport.send_battle_notice.side_effect=interrupted_send
        sending=asyncio.create_task(self.games.notify(v['id']))
        await entered.wait();sending.cancel()
        await asyncio.gather(sending,return_exceptions=True)
        self.assertEqual(self.games.store.get(v['id'])['notice'],'unknown')
        await self.games.tick()
        self.transport.send_battle_notice.assert_awaited_once()

    async def test_replayed_start_does_not_reopen_completed_local_binding(self):
        v=await self.start();self.client.finish();await self.games.refresh(self.games.store.get(v['id']))
        self.client.view=None;self.client.ops={}
        result=await (await self.turn()).execute('pokemon_battle',{'action':'start'})
        self.assertEqual(result['phase'],'ended')
        self.assertIsNone(self.client.view)

    async def test_context_expires_and_render_never_needs_model(self):
        v=await self.start();row=self.games.store.get(v['id']);row.update(context={'context_token':'secret'},context_until=time.time()-1);self.games.store.save(row)
        self.games.store.prune();self.assertEqual(self.games.store.get(v['id'])['context'],{})
        self.assertIn('对战环境',render_battle(v))

    async def test_watcher_only_checks_active_and_pending_records(self):
        v=await self.start()
        for i,notice in enumerate(('sent','failed','unknown','pending')):
            row=self.games.store.create('history-'+str(i),'old-owner',self.message,
                {'id':'history-'+str(i),'phase':'ended','summary':'旧局结束'})
            row['notice']=notice;self.games.store.save(row)
        self.assertEqual({r['id'] for r in self.games.store.due()},{v['id'],'history-3'})
        with patch.object(self.games.store,'all',side_effect=AssertionError('no full-history decode')):
            await self.games.tick()
            self.assertEqual(self.games.store.get(v['id'])['view']['phase'],'rules')
        self.transport.send_battle_notice.assert_awaited_once()


class BotGameTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=wx.WeChatCoreTests.asyncSetUp
    asyncTearDown=wx.WeChatCoreTests.asyncTearDown
    process=wx.WeChatCoreTests.process
    sent_text=wx.WeChatCoreTests.sent_text

    def enable(self):
        self.bot._settings=replace(self.settings,pokemon_dex_enabled=True,pokemon_battle_enabled=True)
        self.cb=FakeCB()
        self.bot._pokemon=PokemonGames(self.bot._settings,self.store,self.cb,self.transport)

    async def test_start_then_selection_rendered_and_model_failure_fallback(self):
        self.enable()
        async def start(system,prompt,tools,execute,**kwargs):
            self.assertIn('pokemon_battle',{t['name'] for t in tools})
            await execute('pokemon_battle',{'action':'start'})
            await execute('pokemon_battle',{'action':'start'})
            raise LLMError('failed after successful tool')
        self.llm.complete_with_tools.side_effect=start
        await self.process(wx.message('来一场宝可梦对战',identity=10))
        self.assertIn('对战环境',self.sent_text());self.assertNotIn('你的六只候选：',self.sent_text())
        async def configure(system,prompt,tools,execute,**kwargs):
            self.assertIn('rules',prompt)
            await execute('pokemon_battle',{'action':'configure','environment':'gen9'})
            return '来，选好你的队伍吧。'
        self.llm.complete_with_tools.side_effect=configure
        await self.process(wx.message('太晶化环境',identity=12))
        self.assertIn('六只候选',self.sent_text());self.assertIn('速度↑10%',self.sent_text())
        self.assertEqual(self.sent_text().count('正在准备'),1)
        async def select(system,prompt,tools,execute,**kwargs):
            self.assertIn('selecting',prompt)
            await execute('pokemon_battle',{'action':'select','selection':[1,3,5]})
            return '开始啦。'
        self.llm.complete_with_tools.side_effect=select
        await self.process(wx.message('选 1 3 5',identity=11))
        self.assertIn('十万伏特',self.sent_text())

    async def test_system_role_and_tool_response_use_same_ownership(self):
        self.enable()
        async def controller(system,prompt,tools,execute,**kwargs):
            self.assertIn('是你自己的对战能力',system)
            result=json.loads(await execute('pokemon_battle',{'action':'start'}))
            self.assertEqual(result['human_player']['candidate_pool'],[])
            self.assertIn('你本人',result['assistant_player']['identity'])
            self.assertNotIn('team',result);self.assertNotIn('id',result)
            return '来，选好环境就和我打一场！'
        self.llm.complete_with_tools.side_effect=controller
        await self.process(wx.message('来一场宝可梦对战',identity=80))
        self.assertIn('我另外随机三只',self.sent_text())

    async def test_postgame_question_receives_facts_and_is_not_replaced_by_summary(self):
        self.enable()
        async def controller(system,prompt,tools,execute,**kwargs):
            await execute('pokemon_battle',{'action':'start'});return '来吧'
        self.llm.complete_with_tools.side_effect=controller
        await self.process(wx.message(identity=81))
        games=self.bot._pokemon;self.cb.finish()
        row=games.store.get(self.cb.view['id']);await games.refresh(row)
        await games.notify(row['id'])
        summaries=self.sent_text().count('宝可梦对战结束')
        async def followup(system,prompt,tools,execute,**kwargs):
            self.assertIn('仅供复盘',prompt);self.assertIn('human_player',prompt)
            facts=json.loads(await execute('pokemon_battle',{'action':'status'}))
            self.assertEqual(facts['result']['winner'],'human_player')
            return '六候选是你的，我的三只是另外随机的；刚才我把归属说反了。'
        self.llm.complete_with_tools.side_effect=followup
        await self.process(wx.message('你哪来的苍响？',identity=82))
        self.assertIn('六候选是你的',self.sent_text())
        self.assertEqual(self.sent_text().count('宝可梦对战结束'),summaries)
        self.assertEqual(games.store.get(row['id'])['notice'],'sent')

    async def test_closing_prompt_excludes_old_narration_and_identifies_winner(self):
        self.enable();self.llm.complete.return_value='这次我赢啦，下次再来！'
        result=await self.bot._pokemon_closing({'phase':'ended','winner':'p2','summary':'我获胜了！',
            'team':[{'name':'水君'}],'opponent':{'name':'苍响'},'closing':'旧的错误归属台词'})
        system,prompt=self.llm.complete.await_args.args[:2]
        self.assertIn('是你自己的对战能力',system)
        self.assertIn('assistant_player',prompt);self.assertNotIn('旧的错误归属台词',prompt)
        self.assertEqual(result,'这次我赢啦，下次再来！')

    async def test_busy_blocks_commands_media_and_llm_before_processing(self):
        self.enable()
        self.cb.owner='someone-else';self.cb.view={'phase':'selecting'}
        for i,text in enumerate(('你好','皮卡丘资料','画一只猫','/reset','来一场宝可梦对战')):
            await self.process(wx.message(text,identity=20+i))
        self.llm.complete_with_tools.assert_not_awaited();self.llm.complete.assert_not_awaited()
        self.assertEqual(self.sent_text().count(BUSY),5)

    async def test_unmentioned_command_is_still_blocked_while_busy(self):
        self.enable();self.cb.owner='other';self.cb.view={'phase':'active'}
        self.transport.bind(wx.message('/reset',identity=99))
        message,_=self.transport.normalize('老师')
        await self.bot.handle_message(replace(message,should_answer=False),_from_task_queue=True)
        self.assertIn(BUSY,self.sent_text());self.llm.complete_with_tools.assert_not_awaited()

    async def test_busy_also_blocks_wechat_unsupported_and_interrupted_replies(self):
        self.enable();self.cb.owner='other';self.cb.view={'phase':'active'}
        await self.process(wx.message(identity=100,item_list=[{'type':3}]))
        self.store.accept({'msgs':[wx.message(identity=101)]})
        self.store.mark('101','running');self.store.recover()
        await self.runtime.process_one()
        self.assertEqual(self.sent_text().count(BUSY),2)
        self.llm.complete_with_tools.assert_not_awaited()

    async def test_final_notice_and_background_tick_do_not_duplicate(self):
        self.enable()
        actions=[{'action':'start'},{'action':'configure','environment':'gen9'},{'action':'select','selection':[1,2,3]},{'action':'act','action_id':'3-move-1'}]
        async def controller(system,prompt,tools,execute,**kwargs):
            result=json.loads(await execute('pokemon_battle',actions.pop(0)))
            if result.get('phase')=='ended': await self.bot._pokemon.tick()
            return '解说'
        self.llm.complete_with_tools.side_effect=controller
        await self.process(wx.message(identity=30));await self.process(wx.message('太晶',identity=33));await self.process(wx.message('选123',identity=31))
        self.cb.end_on_move=True
        await self.process(wx.message('第一招',identity=32))
        self.assertEqual(self.sent_text().count('宝可梦对战结束'),1)

    async def test_wechat_notice_does_not_replace_active_and_checks_destination(self):
        self.enable()
        payload=wx.message(identity=50)
        self.transport.bind(payload)
        context=self.transport.battle_reply_context()
        self.transport.active=None
        await self.transport.send_battle_notice(self.transport.conversation_id,'本局超时','one',context=context)
        self.assertIsNone(self.transport.active)
        await self.transport.send_battle_notice(self.transport.conversation_id,'本局超时','one',context=context)
        self.client.send.assert_awaited_once()
        with self.assertRaises(BattleNoticeRejected): await self.transport.send_battle_notice('other','bad','two',context=context)
        with self.assertRaises(BattleNoticeRejected): await self.transport.send_battle_notice(self.transport.conversation_id,'bad','two',context={**context,'from_user_id':'impostor'})

    async def test_status_discovering_terminal_claims_notice_before_watcher(self):
        self.enable()
        async def start(system,prompt,tools,execute,**kwargs):
            await execute('pokemon_battle',{'action':'start'});return '选队吧'
        self.llm.complete_with_tools.side_effect=start
        await self.process(wx.message(identity=60))
        async def status(system,prompt,tools,execute,**kwargs):
            self.cb.finish()
            await execute('pokemon_battle',{'action':'status'});return '已经结束'
        self.llm.complete_with_tools.side_effect=status
        await self.process(wx.message('看看状态',identity=61))
        await self.bot._pokemon.tick()
        self.assertEqual(self.sent_text().count('宝可梦对战结束'),1)
        self.assertEqual(self.bot._pokemon.store.get(self.cb.view['id'])['notice'],'sent')

    async def test_requested_unknown_result_survives_model_failure(self):
        self.enable()
        async def start(system,prompt,tools,execute,**kwargs):
            await execute('pokemon_battle',{'action':'start'});return '选队吧'
        self.llm.complete_with_tools.side_effect=start
        await self.process(wx.message(identity=70))
        games=self.bot._pokemon;self.cb.finish()
        row=games.store.get(self.cb.view['id']);await games.refresh(row)
        row=games.store.get(row['id']);row['notice']='unknown';games.store.save(row)
        async def status(system,prompt,tools,execute,**kwargs):
            await execute('pokemon_battle',{'action':'status'});raise LLMError('commentary unavailable')
        self.llm.complete_with_tools.side_effect=status
        await self.process(wx.message('刚才的结果呢',identity=71))
        self.assertEqual(self.sent_text().count('宝可梦对战结束'),1)


class HTTPClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_notice_distinguishes_rejection_from_server_uncertainty(self):
        async def handler(request):
            return web.json_response({'error':'test'},status=int(request.match_info['code']))
        app=web.Application();app.router.add_post('/{code}/v2/groups/{group}/messages',handler)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        try:
            port=site._server.sockets[0].getsockname()[1]
            async with ClientSession() as session:
                for code,rejected in ((403,True),(500,False)):
                    client=QQClient(SimpleNamespace(qq_api_base=f'http://127.0.0.1:{port}/{code}'),session)
                    client.auth_headers=AsyncMock(return_value={})
                    with self.assertRaises(QQAPIError) as caught: await client.send_group_text('test','summary','inbound')
                    self.assertEqual(caught.exception.delivery_rejected,rejected)
                    with self.assertRaises(TransportError) as notice: await QQTransport(client).send_battle_notice('test','summary','id')
                    self.assertEqual(isinstance(notice.exception,BattleNoticeRejected),rejected)
        finally: await runner.cleanup()

    async def test_chunked_response_and_http_error_are_bounded_and_sanitized(self):
        async def handler(request):
            if request.path=='/error': return web.json_response({'error':'busy','message':'secret-provider-data'},status=409)
            response=web.StreamResponse(headers={'content-type':'application/json'})
            await response.prepare(request)
            for part in (b'{"ok":',b'true}'):
                await response.write(part);await asyncio.sleep(.01)
            await response.write_eof();return response
        app=web.Application();app.router.add_route('*','/{path:.*}',handler)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        try:
            port=site._server.sockets[0].getsockname()[1]
            async with ClientSession() as session:
                client=PokemonClient(session,f'http://127.0.0.1:{port}','token')
                self.assertEqual(await client.request('GET','/ok'),{'ok':True})
                with self.assertRaisesRegex(PokemonError,BUSY): await client.request('POST','/error',data={})
        finally: await runner.cleanup()
