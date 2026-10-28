"""CB HTTP boundary, trusted chat bindings and durable terminal notices."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import quote

import aiohttp

from .channel import TransportError, BattleNoticeRejected
from .workspace import WorkspaceError

LOGGER = logging.getLogger(__name__)
BUSY = '正在和其他人进行宝可梦对战，等结束再说吧。'
TERMINAL = {'ended', 'cancelled', 'expired', 'failed', 'interrupted'}

TOOLS = [
    {'name': 'pokemon_dex', 'description': '查询 ChangeBattle 宝可梦图鉴。先 search 获取英文 id；多项命中需区分形态。detail 查询种族值、进化、招式、特性或道具。只代表当前项目数据。',
     'input_schema': {'type': 'object', 'properties': {
         'action': {'type': 'string', 'enum': ['search', 'detail']},
         'category': {'type': 'string', 'enum': ['pokemon', 'moves', 'abilities', 'items']},
         'query': {'type': 'string'}, 'id': {'type': 'string'},
         'section': {'type': 'string', 'enum': ['basic', 'learnset']}, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10000}},
         'required': ['action', 'category'], 'additionalProperties': False}},
    {'name': 'pokemon_battle', 'description': '与用户进行随机宝可梦对战。start 开局生成六候选；select 按用户顺序选三个不同编号；act 只能转交用户明确指定的当前合法 action_id，绝不能替用户或对手选择；status 查看；end 取消或认输。同一消息最多一次变更。',
     'input_schema': {'type': 'object', 'properties': {
         'action': {'type': 'string', 'enum': ['start', 'select', 'act', 'status', 'end']},
         'selection': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 6}, 'minItems': 3, 'maxItems': 3, 'uniqueItems': True},
         'action_id': {'type': 'string'}}, 'required': ['action'], 'additionalProperties': False}},
]


class PokemonError(WorkspaceError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class PokemonClient:
    def __init__(self, session, url, token):
        self.session, self.url, self.token = session, url.rstrip('/'), token
        if not self.url.startswith(('http://', 'https://')) or not token:
            raise ValueError('启用宝可梦能力需要内部服务 URL 和 token')

    async def request(self, method, path, *, owner='', instance='', data=None, params=None):
        headers = {'Authorization': 'Bearer ' + self.token,
                   'x-changebattle-chat-owner': owner, 'x-changebattle-instance': instance}
        try:
            async with self.session.request(method, self.url + path, headers=headers, json=data,
                    params=params, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False) as response:
                if response.content_length and response.content_length > 256 * 1024:
                    raise PokemonError('uncertain' if method == 'POST' else 'unavailable', '宝可梦服务响应过大')
                chunks = []; size = 0
                async for chunk in response.content.iter_chunked(65536):
                    chunks.append(chunk); size += len(chunk)
                    if size > 256 * 1024: raise PokemonError('uncertain' if method == 'POST' else 'unavailable', '宝可梦服务响应过大')
                raw = b''.join(chunks)
                result = json.loads(raw)
                if not isinstance(result, dict): raise ValueError('response shape')
                if response.status >= 300:
                    code = result.get('error', 'unavailable')
                    if method == 'POST' and response.status >= 500 and code != 'pokemon_disabled':
                        raise PokemonError('uncertain', '对战操作结果待确认，请稍后查状态；不会重复出招。')
                    messages = {'busy': BUSY, 'stale': '战况已变化，请查看新选项后再发指令。',
                        'instance_changed': '对战服务已重启，本局中断，不判胜负。',
                        'not_found': '本局或图鉴记录不存在。', 'invalid_selection': '请选择三个不同编号（1～6），第一只是首发。',
                        'invalid_action': '请选择当前合法招式或换人选项。', 'operation_conflict': '同一消息不能执行不同对战操作。',
                        'start_expired': '这条开局请求已过期，请重新开局。'}
                    raise PokemonError(code, messages.get(code, '宝可梦服务暂时不可用，请稍后再试。'))
                return result
        except PokemonError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise PokemonError('uncertain' if method == 'POST' else 'unavailable',
                '对战操作结果待确认，请稍后查状态；我不会重复出招。' if method == 'POST' else '暂时无法查询宝可梦服务，请稍后再试。') from None


class BattleStore:
    """One record per battle so a new game cannot overwrite an unsent result."""
    def __init__(self, store):
        self.db = store._db
        self.db.execute('''CREATE TABLE IF NOT EXISTS game_battle_bindings(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, channel TEXT NOT NULL,
            conversation TEXT NOT NULL, user_id TEXT NOT NULL, view TEXT NOT NULL,
            pending TEXT NOT NULL DEFAULT '{}', notice TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '{}', context_until REAL NOT NULL DEFAULT 0,
            updated REAL NOT NULL, event_cursor INTEGER NOT NULL DEFAULT 0)''')
        self.db.execute("UPDATE game_battle_bindings SET notice='unknown' WHERE notice='sending'")
        self.db.commit()

    def all(self):
        cursor = self.db.execute('SELECT * FROM game_battle_bindings ORDER BY updated')
        return [self.decode(dict(zip([d[0] for d in cursor.description], r))) for r in cursor.fetchall()]

    def get(self, identity):
        cursor = self.db.execute('SELECT * FROM game_battle_bindings WHERE id=?', (identity,))
        row = cursor.fetchone()
        return self.decode(dict(zip([d[0] for d in cursor.description], row))) if row else None

    def due(self):
        cursor = self.db.execute("SELECT * FROM game_battle_bindings WHERE notice IN ('','pending') ORDER BY updated")
        return [self.decode(dict(zip([d[0] for d in cursor.description], r))) for r in cursor.fetchall()]

    @staticmethod
    def decode(row):
        for k in ('view', 'pending', 'context'): row[k] = json.loads(row[k])
        return row

    def save(self, row):
        row['updated'] = time.time()
        fields = ('id','owner','channel','conversation','user_id','view','pending','notice','context','context_until','updated','event_cursor')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO game_battle_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                [json.dumps(row[k], ensure_ascii=False) if k in ('view','pending','context') else row[k] for k in fields])

    def create(self, identity, owner, message, view):
        row = dict(id=identity, owner=owner, channel=message.channel, conversation=message.conversation_id,
                   user_id=message.user_id, view=view, pending={}, notice='', context={}, context_until=0,
                   updated=time.time(), event_cursor=0)
        self.save(row)
        return row

    def claim_notice(self, identity):
        with self.db:
            return self.db.execute("UPDATE game_battle_bindings SET notice='sending' WHERE id=? AND notice IN ('pending','failed')", (identity,)).rowcount == 1

    def prune(self):
        with self.db:
            self.db.execute("UPDATE game_battle_bindings SET context='{}',context_until=0 WHERE context_until>0 AND context_until<?", (time.time(),))
            self.db.execute("DELETE FROM game_battle_bindings WHERE updated<? AND notice='sent'", (time.time()-7*86400,))


def render_battle(view, cursor=0):
    if view.get('phase') in TERMINAL: return view.get('summary') or '本局已中断，不判胜负。'
    if view.get('phase') == 'selecting':
        lines = ['你的六只候选：']
        for p in view.get('candidates', []):
            lines.append(f"{p['slot']}. {p['name']} Lv.{p['level']}：{' / '.join(p['moves'])}")
        return '\n'.join(lines + ['选三个不同编号，第一只是首发，例如“选 2、5、1”。5 分钟不操作会结束。'])
    lines = [f"第 {view.get('turn', 0)} 回合"]
    lines += [e['text'] for e in view.get('events', []) if e['id'] > cursor]
    opponent = view.get('opponent') or {}
    if opponent: lines.append(f"对手：{opponent.get('name', '')} {opponent.get('condition', '')}")
    lines += [f"{'出场' if p['active'] else '后备'} {p['slot']}. {p['name']} {p['condition']}" for p in view.get('team', [])]
    lines += [a['label'] for a in view.get('actions', [])]
    if not view.get('actions'): lines.append('等待引擎结算；可以稍后查看战况。')
    return '\n'.join(lines)[-3000:]


class PokemonGames:
    def __init__(self, settings, store, client, transport):
        self.settings, self.store, self.client, self.transport = settings, BattleStore(store), client, transport
        self.locks = {}
        self.task = None

    def owner(self, message):
        if not message.user_id or message.user_id == 'unknown' or not message.conversation_id:
            raise PokemonError('identity', '无法确认发言人，不能操作对局。')
        scope = json.dumps([message.channel, message.conversation_id, message.user_id], ensure_ascii=False)
        return hmac.new(self.client.token.encode(), scope.encode(), hashlib.sha256).hexdigest()

    def lock(self, identity):
        return self.locks.setdefault(identity, asyncio.Lock())

    async def gate(self, message):
        if not self.settings.pokemon_battle_enabled: return {'status': 'free'}
        owner = self.owner(message)
        result = await self.client.request('GET', '/chat-battles/availability', owner=owner)
        if result['status'] == 'self':
            view = result['battle']; row = self.store.get(view['id']) or self.store.create(view['id'], owner, message, view)
            row['view'] = view; self.capture(row); self.store.save(row)
        return result

    def capture(self, row):
        context = getattr(self.transport, 'battle_reply_context', lambda: {})()
        if context and row['channel'] == 'wechat':
            row['context'] = context
            row['context_until'] = time.time() + 360

    def update(self, row, view):
        row['view'] = view
        if view.get('phase') in TERMINAL and not row['notice']: row['notice'] = 'pending'
        self.store.save(row)

    def interrupted(self, row):
        self.update(row, {**row['view'], 'phase': 'interrupted', 'summary': '对战服务已重启或本局已失效，本局中断，不判胜负。', 'actions': []})
        row['pending'] = {}; self.store.save(row)

    async def refresh(self, row):
        try:
            view = await self.client.request('GET', '/chat-battles/' + row['id'], owner=row['owner'], instance=row['view']['instanceId'],
                                            params={'operationId': row['pending']['data']['operationId']} if row['pending'] else None)
        except PokemonError as exc:
            if exc.code == 'instance_changed': self.interrupted(row); return row['view']
            if exc.code == 'not_found':
                if row['pending'] and row['pending']['kind'] == 'start':
                    # Retry only the same persisted key and epoch, never create a replacement.
                    return await self.submit_pending(row)
                self.interrupted(row); return row['view']
            raise
        operation = view.pop('operation', None)
        if row['pending']:
            if operation and operation.get('status') in ('done', 'rejected'): row['pending'] = {}
            elif operation and operation.get('status') == 'not_found': return await self.submit_pending(row)
        self.update(row, view)
        return view

    async def submit_pending(self, row):
        pending = row['pending']
        try:
            view = await self.client.request('POST', pending['path'], owner=row['owner'], data=pending['data'])
        except PokemonError as exc:
            if exc.code in ('instance_changed', 'start_expired', 'not_found'):
                self.interrupted(row); return row['view']
            if exc.code != 'uncertain':
                row['pending'] = {}
                if pending['kind'] == 'start':
                    row['view'] = {**row['view'], 'phase': 'cancelled'}; row['notice'] = 'sent'; row['context'] = {}
                self.store.save(row)
            raise
        if view['id'] != row['id']:
            # A concurrent request by the same owner may have already opened a game.
            old = row
            old['pending'] = {}; old['view'] = {**old['view'], 'phase': 'cancelled'}
            old['notice'] = 'sent'; old['context'] = {}; self.store.save(old)
            row = self.store.get(view['id']) or {**old, 'id': view['id'], 'notice': ''}
        row['pending'] = {}; self.update(row, view)
        return view

    async def notify(self, identity, message=None, sequence=1):
        async with self.lock(identity):
            row = self.store.get(identity)
            if not row or row['view'].get('phase') not in TERMINAL: return False
            if message and (message.channel, message.conversation_id, message.user_id) != (row['channel'], row['conversation'], row['user_id']): return False
            if row['notice'] == 'failed' and message is None: return False
            if not self.store.claim_notice(identity): return False
            row['notice'] = 'sending'
            try:
                if message:
                    await self.transport.send_text(row['conversation'], render_battle(row['view']), message.message_id, sequence=sequence)
                else:
                    context = row['context'] if row['context_until'] >= time.time() else {}
                    await self.transport.send_battle_notice(row['conversation'], render_battle(row['view']), 'battle-' + identity, context=context)
            except asyncio.CancelledError:
                row['notice'] = 'unknown'
                raise
            except BattleNoticeRejected:
                row['notice'] = 'failed'
            except Exception as exc:
                from .qq import QQAPIError
                row['notice'] = 'failed' if isinstance(exc, QQAPIError) and exc.delivery_rejected else 'unknown'
            else:
                row['notice'] = 'sent'
            finally:
                if row['notice'] != 'sending':
                    row['context'] = {}; row['context_until'] = 0; self.store.save(row)
            return row['notice'] == 'sent'

    async def pending_notices(self, message):
        for row in self.store.all():
            if (row['channel'],row['conversation'],row['user_id']) == (message.channel,message.conversation_id,message.user_id) and row['notice'] in ('pending','failed'):
                sent = await self.notify(row['id'], message)
                if self.store.get(row['id'])['notice'] == 'unknown': return None
                return sent
        return False

    async def tick(self):
        self.store.prune()
        for record in self.store.due():
            identity = record['id']
            try:
                async with self.lock(identity):
                    row = self.store.get(identity)
                    if row['view'].get('phase') not in TERMINAL: await self.refresh(row)
                await self.notify(identity)
            except PokemonError:
                LOGGER.warning('宝可梦状态暂不可用，保留绑定和未确认操作')
            except Exception:
                LOGGER.warning('宝可梦通知维护失败，保留恢复记录')

    def start(self):
        if not self.task and self.settings.pokemon_battle_enabled:
            self.task = asyncio.create_task(self.run(), name='pokemon-battle-notices')

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(5)

    async def close(self):
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task, return_exceptions=True); self.task = None

    def turn(self, message, availability): return PokemonTurn(self, message, availability)


class PokemonTurn:
    def __init__(self, games, message, availability):
        self.games, self.message, self.availability = games, message, availability
        self.view = availability.get('battle')
        self.last_view = None
        self.mutation = None
        self.query_count = 0
        self.is_status = False
        self.cursor = (games.store.get(self.view['id']) or {}).get('event_cursor', 0) if self.view else 0

    def prompt(self):
        return ('\n[宝可梦对局]\n' + json.dumps(self.view, ensure_ascii=False) if self.view else '\n[宝可梦对局] 当前没有你的活动对局。') + (
            '\n只有 pokemon_battle 的真实结果能证明开局/选队/出招已执行。对手由 CB AI 操作，你不能替双方出招。'
            '用户意图不明确先澄清。动作必须选择本轮视图的 action id；不按旧历史猜编号。'
            '对战工具执行后用一句话解说即可，程序会附候选或战况。普通聊天/图鉴查询不调用出招。')

    async def execute(self, name, args):
        if not isinstance(args, dict): raise PokemonError('input', '工具参数必须是对象')
        if name == 'pokemon_dex':
            if not self.games.settings.pokemon_dex_enabled: raise PokemonError('disabled', '图鉴未启用')
            if set(args) - {'action','category','query','id','section','offset'}: raise PokemonError('input', '不支持的图鉴参数')
            if args.get('category') not in ('pokemon','moves','abilities','items') or args.get('action') not in ('search','detail'): raise PokemonError('input', '图鉴操作或分类无效')
            if self.query_count >= 4: raise PokemonError('limit', '本条消息图鉴查询次数已用完')
            self.query_count += 1
            offset = args.get('offset', 0)
            if type(offset) is not int or not 0 <= offset <= 10000: raise PokemonError('input', '页码无效')
            if args['action'] == 'search':
                query = args.get('query','')
                if not isinstance(query,str) or len(query) > 120: raise PokemonError('input','查询文字过长')
                return await self.games.client.request('GET','/dex/search',params={'category':args['category'],'query':query,'offset':offset,'limit':10})
            identity = args.get('id', '')
            if not isinstance(identity, str) or not identity.isascii() or not identity.replace('-','').isalnum() or len(identity)>100: raise PokemonError('input','请先搜索取得英文图鉴 ID')
            section = args.get('section','basic')
            if section not in ('basic','learnset'): raise PokemonError('input','不支持的详情分类')
            return await self.games.client.request('GET','/dex/'+args['category']+'/'+quote(identity),params={'section':section,'offset':offset})
        if name != 'pokemon_battle' or not self.games.settings.pokemon_battle_enabled: raise PokemonError('disabled','对战工具未启用')
        if set(args) - {'action','selection','action_id'}: raise PokemonError('input','不支持的对战参数')
        action = args.get('action')
        if action not in ('start','select','act','status','end'): raise PokemonError('input','对战操作无效')
        owner = self.games.owner(self.message)
        if action == 'status':
            self.is_status = True
            if not self.view:
                recent = [r for r in self.games.store.all() if r['owner'] == owner and r['view'].get('summary')]
                if recent:
                    self.last_view = recent[-1]['view']
                    return self.last_view
                return {'message':'当前没有你的活动对局。'}
            async with self.games.lock(self.view['id']):
                self.last_view = await self.games.refresh(self.games.store.get(self.view['id']))
            # A read does not silently update this turn's command precondition.
            return self.last_view
        fingerprint = json.dumps(args, sort_keys=True, ensure_ascii=False)
        if self.mutation:
            if self.mutation != fingerprint: raise PokemonError('limit','每条消息只能执行一次对战变更')
            if self.last_view: return self.last_view
            raise PokemonError('pending','本次操作已提交，请稍后查状态，不会重复执行')
        self.mutation = fingerprint
        if action == 'start' and self.view:
            self.last_view = self.view; return self.view
        if action == 'start':
            identity = hashlib.sha256((owner+':'+self.message.message_id).encode()).hexdigest()
            row = self.games.store.get(identity)
            if not row:
                row = self.games.store.create(identity, owner, self.message, {'id':identity,'instanceId':self.availability['instanceId'],'phase':'preparing','revision':0})
        else:
            if not self.view: raise PokemonError('missing','请先开始一局宝可梦对战')
            identity = self.view['id']; row = self.games.store.get(identity)
        async with self.games.lock(identity):
            row = self.games.store.get(identity)
            if row['pending']: raise PokemonError('pending','上次操作结果待确认，请先查状态')
            if action == 'start' and row['view']['phase'] != 'preparing':
                self.last_view = row['view']; return self.last_view
            data = {'instanceId':row['view']['instanceId'], 'operationId':hashlib.sha256((identity+':'+self.message.message_id).encode()).hexdigest()}
            if action == 'start': data.update(id=identity, issuedAt=int(time.time()*1000)); path='/chat-battles'
            else:
                data['revision'] = self.view['revision']
                path='/chat-battles/'+identity+'/'+{'select':'select','act':'actions','end':'end'}[action]
                if action == 'select':
                    selected=args.get('selection')
                    if not isinstance(selected,list) or len(selected)!=3 or any(type(n) is not int or not 1<=n<=6 for n in selected) or len(set(selected))!=3: raise PokemonError('input','请选择三个不同编号（1～6）')
                    data['selection']=selected
                if action == 'act':
                    action_id=args.get('action_id')
                    if action_id not in [a['id'] for a in self.view.get('actions',[])]: raise PokemonError('input','请选择本轮提供的合法动作')
                    data['actionId']=action_id
            row['pending']={'kind':action,'path':path,'data':data}
            self.games.capture(row); self.games.store.save(row)
            self.last_view=await self.games.submit_pending(row)
            return self.last_view
