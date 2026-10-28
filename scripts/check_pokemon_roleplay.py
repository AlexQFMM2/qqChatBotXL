"""Opt-in configured-model roleplay probe using synthetic CB views and mocked transports.

Uses the configured LLM only; never connects to CB or sends platform messages.
Printed prose is evidence for manual semantic review, not a proof of all model outputs.
"""
import asyncio
import copy
import json
import logging
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.bot import PersonaBot
from app.channel import IncomingMessage
from app.config import Settings
from app.llm import LLMClient
from app.pokemon import PokemonGames, TERMINAL
from app.storage import MemoryStore

logging.basicConfig(level=logging.CRITICAL)


def pokemon(slot, name, types, moves):
    return {'slot': slot, 'name': name, 'level': 50, 'types': types, 'moves': moves,
            'nature': '认真', 'natureEffect': '无能力增减', 'ability': '已记录', 'item': '无'}


CANDIDATES = [pokemon(i+1, name, types, moves) for i, (name, types, moves) in enumerate([
    ('九尾', ['火'], ['大字爆炎']), ('水君', ['水'], ['热水','替身','冥想','冰冻光束']),
    ('向日花怪', ['草'], ['日光束']), ('烈焰猴', ['火','格斗'], ['近身战','过热']),
    ('乐天河童', ['水','草'], ['水炮']), ('卡比兽', ['一般'], ['诅咒','地震']),
])]
TEAM = [{**CANDIDATES[i], 'slot': n+1, 'condition': '100/100', 'active': n == 0} for n,i in enumerate([3,1,5])]
BASE = dict(id='synthetic-roleplay-battle', instanceId='synthetic-instance', phase='rules', revision=1,
            turn=0, winner=None, summary='', reason='', expiresAt=0, events=[], team=[], actions=[],
            candidates=[], environment={'id':'standard','label':'普通对战'},
            environments=[{'id':'standard','label':'普通对战'}])
SELECTING = {**BASE, 'phase':'selecting','revision':2,'candidates':CANDIDATES}
ACTIVE = {**BASE, 'phase':'active','revision':3,'turn':1,'team':TEAM,'playerCandidates':CANDIDATES,
          'opponent':{'name':'鬃岩狼人','condition':'161/161'},
          'events':[{'id':1,'text':'你方换上烈焰猴'},{'id':2,'text':'我方换上鬃岩狼人'}],
          'actions':[{'id':'3-move-1','label':'第1招 近身战'}]}
FORCED = {**ACTIVE, 'revision':4, 'turn':2,
          'opponent':{'name':'苍响','condition':'178/178'},
          'team':[{**p,'condition':'0 fnt' if i==0 else '100/100'} for i,p in enumerate(TEAM)],
          'events':[{'id':3,'text':'我方换上苍响'},{'id':4,'text':'我方使用了嬉闹'},
                    {'id':5,'text':'你方出场宝可梦倒下了'}],
          'actions':[{'id':'4-switch-2','label':'换第2只 水君'},{'id':'4-switch-3','label':'换第3只 卡比兽'}]}
SWITCHED = {**FORCED, 'revision':5, 'turn':3,
            'team':[{**p,'active':i==2} for i,p in enumerate(FORCED['team'])],
            'events':FORCED['events']+[{'id':6,'text':'你方换上卡比兽'}],
            'actions':[{'id':'5-move-1','label':'第1招 地震'}]}
ENDED = {**SWITCHED, 'phase':'ended', 'revision':6, 'winner':'p2', 'reason':'玩家认输',
         'assistantRevealed':['鬃岩狼人','苍响'], 'actions':[],
         'summary':'宝可梦对战结束：我获胜了！\n回合：3。玩家主动认输。'}


class ScriptedCB:
    token = 'synthetic-roleplay-key'

    def __init__(self, before, after=None, expected=None):
        self.view, self.after, self.expected = copy.deepcopy(before), copy.deepcopy(after), expected
        self.mutations = 0

    async def request(self, method, path, **kwargs):
        if path.endswith('/availability'):
            active = self.view['phase'] not in TERMINAL
            return {'instanceId':self.view['instanceId'], 'status':'self' if active else 'free',
                    **({'battle':copy.deepcopy(self.view)} if active else {})}
        if method == 'POST':
            assert self.after is not None, 'a replay question must not mutate a battle'
            expected_path, expected_data = self.expected
            assert path.endswith(expected_path), 'unexpected operation'
            assert all(kwargs['data'].get(k)==v for k,v in expected_data.items()), 'unexpected choice'
            self.mutations += 1
            self.view = self.after
        return copy.deepcopy(self.view)


async def run_case(session, label, before, question, after=None, expected=None, history=()):
    with tempfile.TemporaryDirectory(prefix='pokemon-roleplay-') as directory:
        settings=replace(Settings.from_env(), database_path=directory+'/chat.db', workspace_root=directory+'/workspace',
            owner_user_ids=frozenset(), good_morning_enabled=False, web_tools_enabled=False, bilibili_enabled=False,
            vision_enabled=False, image_generation_enabled=False, voice_enabled=False, emote_enabled=False,
            pokemon_dex_enabled=False, pokemon_battle_enabled=True, log_message_content=False,
            pokemon_service_url='http://unused.invalid', pokemon_service_token='synthetic-roleplay-key')
        store=MemoryStore(settings.database_path)
        sent=[]
        async def send(_conversation, text, *_args, **_kwargs):
            sent.append(text);return {'id':'synthetic-reply'}
        transport=SimpleNamespace(send_text=send,send_battle_notice=AsyncMock(),uncertain=False,expired=False)
        bot=PersonaBot(settings,None,LLMClient(settings,session),store,transport=transport)
        client=ScriptedCB(before,after,expected)
        games=PokemonGames(settings,store,client,transport,narrator=bot._pokemon_closing)
        bot._pokemon=games
        message=IncomingMessage('qq','synthetic-roleplay-group','probe-'+label,'player','老师','member',question,question)
        row=games.store.create(before['id'],games.owner(message),message,before)
        row['event_cursor']=max([e['id'] for e in before.get('events',[])] or [0]);games.store.save(row)
        if before['phase'] in TERMINAL:
            row['notice']='sent';games.store.save(row)
        try:
            for text in history:
                await store.add(message.conversation_id,'bot','夏莉',text,is_bot=True)
            await asyncio.wait_for(bot.handle_message(message,_from_task_queue=True),120)
            assert sent, 'no reply'
            # Preserve the generated prose while omitting automatic card blocks from the review log.
            reply=sent[-1].split('\n第 ')[0].split('\n环境：')[0].split('\n来，先选')[0]
            assert reply, 'empty commentary'
            print(json.dumps({'case':label,'question':question,'reply':reply,'mutations':client.mutations},ensure_ascii=False),flush=True)
            assert client.mutations == (1 if after else 0), 'mutation count differs'
            if label=='postgame_origin':
                closing=await asyncio.wait_for(bot._pokemon_closing(ENDED),40)
                print(json.dumps({'case':'closing','reply':closing},ensure_ascii=False),flush=True)
        finally:
            await bot.close();store.close()


async def main():
    async with aiohttp.ClientSession() as session:
        await run_case(session,'configure',BASE,'普通对战吧',SELECTING,('/configure',{'environment':'standard'}))
        await run_case(session,'select',SELECTING,'选4、2、6，按顺序出场',ACTIVE,('/select',{'selection':[4,2,6]}))
        await run_case(session,'forced_switch',FORCED,'回来吧烈焰猴，出来吧卡比兽',SWITCHED,('/actions',{'actionId':'4-switch-3'}))
        await run_case(session,'postgame_origin',ENDED,'你初始六只里没有苍响啊，你哪来的苍响？',
            history=['我这边六个候选是九尾、水君、向日花怪、烈焰猴、乐天河童、卡比兽。',
                     '我这边先派上水君，让它多扛几回合。'])
        await run_case(session,'postgame_ownership',ENDED,'你的烈焰猴和卡比兽刚才都倒下了吧？',
            history=['它把我的烈焰猴和卡比兽一块儿收走了。'])
    print('PASS synthetic roleplay execution; inspect each printed reply for semantic acceptance; platform sends=0',flush=True)


if __name__=='__main__':
    asyncio.run(main())
