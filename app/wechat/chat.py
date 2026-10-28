"""Independent WeChat entry using the existing PersonaBot, model and file tools."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
import time
from dataclasses import replace
from pathlib import Path

import aiohttp

from ..bot import PersonaBot
from ..channel import IncomingMessage
from ..config import Settings
from ..llm import LLMClient, LLMError
from ..memory.service import MemoryService
from ..pokemon import BUSY, PokemonError
from ..webtools import WebTools
from .__main__ import DEFAULT_STATE
from .client import ProtocolError, PublicResolver, SessionExpired, WeChatClient
from .inbox import Inbox, InboxFull
from .state import State
from .transport import WeChatTransport

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def failure_details(exc):
    """Only locally known categories; never include raw model/provider errors."""
    cause = exc
    for _ in range(4):
        if cause.__cause__ is None:
            break
        cause = cause.__cause__
    category = type(cause).__name__
    if isinstance(cause, LLMError):
        detail = str(cause)
        status = re.match(r'模型接口 HTTP (\d{3})(?::|$)', detail)
        if status:
            category += ':HTTP_' + status[1]
        elif '限流' in detail:
            category += ':rate_limited'
        elif '超时' in detail:
            category += ':timeout'
        elif '没有返回文本' in detail or '没有文本' in detail:
            category += ':empty_response'
    return category


def load_environment(paths):
    """Read dotenv data without executing shell expressions or changing shell overrides."""
    values = {}
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or not key.strip().replace('_', '').isalnum():
                raise ValueError('环境配置含不支持的行，请使用 KEY=value 格式')
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)


def chat_settings(state, account):
    settings = Settings.from_env(channel='wechat')
    persona = Path(os.getenv('WECHAT_PERSONA_PATH', str(ROOT / 'persona.md')))
    if not persona.is_file():
        raise ValueError('微信人格文件不存在，请设置 WECHAT_PERSONA_PATH')
    # Source checkouts keep these in bq/; Docker copies them to /app/emotes.
    default_emotes = ROOT / 'bq' if (ROOT / 'bq').is_dir() else ROOT / 'emotes'
    emotes = Path(os.getenv('WECHAT_EMOTE_PATH', str(default_emotes)))
    return replace(
        settings, database_path=str(state.directory / 'chat.db'),
        database_backup_dir=str(state.directory / 'backups'),
        workspace_root=str(state.directory / 'workspace'), persona_path=str(persona),
        owner_user_ids=frozenset({account['owner']}), allowed_groups=frozenset(),
        reply_mode='all', good_morning_enabled=False,
        voice_enabled=os.getenv('WECHAT_VOICE_ENABLED', 'false').lower() in {'true','1'},
        voice_max_mb=min(settings.voice_max_mb, 5),
        bilibili_enabled=False, log_message_content=False,
        emote_path=str(emotes), emote_enabled=settings.emote_enabled and emotes.is_dir(),
        web_tools_enabled=os.getenv('WECHAT_WEB_TOOLS_ENABLED', 'false').lower() in {'true','1'},
        max_workspace_file_mb=min(settings.max_workspace_file_mb, 5),
    )


class ChatRuntime:
    def __init__(self, client, inbox, transport, bot, stop, state=None):
        self.client, self.inbox, self.transport, self.bot, self.stop = client, inbox, transport, bot, stop
        self.wakeup = asyncio.Event()
        self.state = state
        self.login_ready = True
        self.last_poll = None
        self.last_error = ''
        self.started = time.time()
        self.active = False

    async def receive(self):
        delay = 1
        while not self.stop.is_set() and self.login_ready:
            try:
                batch = await self.client.updates(self.inbox.meta('cursor'))
                self.last_poll = time.time()
                self.last_error = ''
                accepted = self.inbox.accept(batch)
                if accepted:
                    LOGGER.info('微信收件已落盘：%d 条', accepted)
                    self.wakeup.set()
                delay = 1
                await asyncio.sleep(.2)
            except SessionExpired:
                self.login_ready = False
                self.last_error = 'SessionExpired'
                LOGGER.warning('微信登录失效，已停止轮询；进程保留健康状态，等待维护者重新登录。')
                await self.stop.wait()
                return
            except (ProtocolError, InboxFull):
                self.last_error = 'poll_unavailable'
                LOGGER.warning('微信收件暂不可用，%s 秒后重试；未丢弃待处理消息', delay)
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 30)
        if not self.login_ready:
            await self.stop.wait()

    async def process_one(self):
        row = self.inbox.next()
        if row is None:
            return False
        self.active = True
        identity, payload, status = row
        self.transport.bind(payload, recovery=status == 'interrupted')
        self.inbox.mark(identity, 'running')
        try:
            if status == 'interrupted':
                await self.reply_without_controller(
                    '上次处理期间服务重启了，结果可能尚未送达。为避免重复执行，我没有自动重做；'
                    '可以用 /files 检查已生成文件，再发消息告诉我如何继续。', identity)
            else:
                message, rejection = self.transport.normalize(self.bot._settings.owner_title)
                if rejection:
                    await self.reply_without_controller(rejection, identity)
                else:
                    # The durable channel queue owns ordering and recovery. Reuse the
                    # complete common handler, without a second in-memory QQ queue.
                    await self.bot.handle_message(message, _from_task_queue=True)
            self.inbox.mark(identity, 'unknown' if self.transport.uncertain else 'done')
            LOGGER.info('微信本条处理结束：%s', '发送待核对' if self.transport.uncertain else '已确认 API 成功')
        except asyncio.CancelledError:
            raise  # Persisted running/sending states are recovered at next startup.
        except Exception as exc:
            # Never print provider payloads, message bodies, context tokens or keys.
            LOGGER.warning('微信本条处理未完成：%s', failure_details(exc))
            if not self.transport.uncertain and not self.transport.expired and not getattr(exc, 'notified', False):
                try:
                    await self.transport.send_text(self.transport.conversation_id,
                        '这次处理没有完成。可以用 /files 查看已经保存的文件，再重新发起请求。', identity)
                except Exception:
                    LOGGER.warning('失败提示未确认送达，请在手机核对')
            self.inbox.mark(identity, 'unknown' if self.transport.uncertain else 'failed')
        finally:
            self.transport.active = None
            self.active = False
        if self.transport.expired:
            self.login_ready = False
            self.last_error = 'SessionExpired'
        return True

    async def reply_without_controller(self, content, identity):
        # Normalization errors and interrupted inputs are also answered entrances.
        games = getattr(self.bot, '_pokemon', None)
        if games:
            message = IncomingMessage('wechat', self.transport.conversation_id, str(identity),
                self.transport.account['owner'], self.bot._settings.owner_title, 'owner', '', '')
            try:
                if (await games.gate(message))['status'] == 'busy': content = BUSY
            except PokemonError as exc:
                content = str(exc)
        await self.transport.send_text(self.transport.conversation_id, content, identity)

    async def work(self):
        while not self.stop.is_set():
            if not self.login_ready:
                await self.stop.wait()
                return
            self.wakeup.clear()
            if await self.process_one():
                continue
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except TimeoutError:
                pass

    async def maintain(self):
        while not self.stop.is_set():
            try:
                await self.bot.run_maintenance()
                self.inbox.prune()
            except Exception as exc:
                LOGGER.warning('微信运行库维护未完成：%s', type(exc).__name__)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=3600)
            except TimeoutError:
                pass

    def health(self):
        statuses = dict(self.inbox._db.execute('SELECT status,count(*) FROM wx_inbox GROUP BY status'))
        unknown = self.inbox._db.execute("SELECT count(*) FROM wx_outbox WHERE status IN ('unknown','sending')").fetchone()[0]
        memory = getattr(self.bot, '_memory', None)
        return dict(alive=True, started_at=self.started, updated_at=time.time(),
                    login_ready=self.login_ready, last_successful_poll=self.last_poll,
                    last_error=self.last_error, active=self.active,
                    backlog=sum(statuses.get(k, 0) for k in ('queued','running','interrupted')),
                    uncertain_sends=unknown, memory=memory.status() if memory else {'enabled': False})

    async def report_health(self):
        while True:
            if self.state:
                self.state.write('health.json', self.health())
            await asyncio.sleep(5)

    async def run(self):
        if self.bot is not None:
            self.bot.start_games()
        self.inbox.recover()
        receiver = asyncio.create_task(self.receive())
        worker = asyncio.create_task(self.work())
        reporter = asyncio.create_task(self.report_health())
        tasks = [receiver, worker, asyncio.create_task(self.maintain()),
                 asyncio.create_task(self.stop.wait()), reporter]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.stop.set()
            receiver.cancel()
            # Finish the current handler before snapshot/cutover. New queued input stays durable.
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=180)
            except (TimeoutError, asyncio.CancelledError):
                worker.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.bot is not None:
                await self.bot.close()
            if self.state:
                self.state.write('health.json', {**self.health(), 'alive': False})


async def run(args):
    os.umask(0o077)
    state = State(args.state_dir)
    load_environment([ROOT / '.env', state.directory / 'model.env', args.env_file])
    # The shared QQ core historically logs rich exceptions. This private entry
    # logs only counts/state/error classes until common logging is fully audited.
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    for name in ('app.bot', 'app.llm', 'app.webtools', 'app.bilibili'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    with state.lock():
        account = state.account()
        if account is None:
            raise ProtocolError('尚未绑定微信，请先运行 scripts/wechat-probe.sh login。')
        settings = chat_settings(state, account)
        dbpath = Path(settings.database_path)
        if dbpath.is_symlink():
            raise ProtocolError('运行库不能是符号链接。')
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        timer = loop.call_later(args.minutes * 60, stop.set) if args.minutes else None
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver())) as wx_session, \
                    aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120, connect=15)) as model_session:
                llm = LLMClient(settings, model_session)
                if args.check:
                    result = await llm.complete('这是连接测试，请只回复 OK。', '测试模型连接。')
                    if not result.strip():
                        raise ValueError('模型连接返回空回复')
                    LOGGER.info('现有 Fusion 模型连接测试成功；未向微信发送消息')
                    return
                probe = state.read('probe.json', {})
                inbox = Inbox(dbpath, account, cursor=probe.get('cursor', ''))
                dbpath.chmod(0o600)
                memory = None
                try:
                    client = WeChatClient(wx_session, base_url=account['base_url'], token=account['token'])
                    transport = WeChatTransport(client, inbox, account)
                    web = WebTools(timeout_seconds=settings.web_request_timeout_seconds,
                        max_bytes=settings.web_fetch_max_kb*1024, max_chars=settings.web_fetch_max_chars,
                        search_results=settings.web_search_results, search_provider=settings.search_provider,
                        searxng_base_url=settings.searxng_base_url) if settings.web_tools_enabled else None
                    memory = MemoryService.from_env(account['bot_id'], inbox, llm, channel='wechat')
                    bot = PersonaBot(settings, None, llm, inbox, web, transport=transport, memory=memory)
                    if memory:
                        memory.start()
                    await bot._seed_builtin_media()
                    state.write('chat-process.json', {'pid': os.getpid(), 'running': True})
                    LOGGER.info('夏莉微信私聊已启动；复用人格 / Fusion / 工作区工具，附件限 5 MiB')
                    LOGGER.info('联网工具：%s；夏莉表情：%s；长期记忆按运行配置启用',
                                bool(web), settings.emote_enabled)
                    await ChatRuntime(client, inbox, transport, bot, stop, state).run()
                finally:
                    if memory:
                        await memory.close()
                    inbox.close()
                    state.write('chat-process.json', {'pid': os.getpid(), 'running': False})
                    LOGGER.info('夏莉微信私聊已停止；待处理消息保留')
        finally:
            if timer:
                timer.cancel()


def main():
    parser = argparse.ArgumentParser(description='夏莉微信私聊，共用 QQChat 人格 / 模型 / 工具')
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.wechat.env')
    parser.add_argument('--check', action='store_true', help='仅验证配置及模型连接，不收发微信')
    parser.add_argument('--minutes', type=int, default=0, help='本次运行分钟数；0 为持续运行')
    args = parser.parse_args()
    if args.minutes < 0:
        parser.error('--minutes 不能小于 0')
    try:
        asyncio.run(run(args))
    except (ValueError, ProtocolError) as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:
        raise SystemExit(f'微信入口未就绪：{type(exc).__name__}；未记录接口正文，请检查配置和网络。') from None


if __name__ == '__main__':
    main()
