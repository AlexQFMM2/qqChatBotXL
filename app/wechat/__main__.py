from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import io
import os
import re
import signal
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .client import (API_BASE, DeliveryUnknown, ProtocolError, PublicResolver,
                     SessionExpired, WeChatClient, text_item, trusted_url)
from .media import Media
from .state import State, atomic_write, confirmed_account

DEFAULT_STATE = Path(__file__).resolve().parents[2] / ".wechat-probe"
HELP = ("微信通道测试已就绪（尚未接入夏莉模型）。\n"
        "发「测试」检查文字；发一张小图片或一个文件检查往返（≤5 MiB）；\n"
        "发「延迟 120」或「延迟 300」检查延迟回复，等待期间请勿发新消息；\n"
        "发「结束测试」停止本次诊断。")


def show_qr(state, content):
    try:
        import qrcode
    except ImportError:
        raise ProtocolError("缺少二维码依赖，请安装 requirements-wechat.txt。") from None
    if not isinstance(content, str) or not content or len(content) > 4096:
        raise ProtocolError("二维码展示内容无效。")
    qr = qrcode.QRCode(box_size=8, border=4)
    qr.add_data(content)
    qr.make(fit=True)
    stream = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(stream, format="PNG")
    atomic_write(state.directory / "login.png", stream.getvalue())
    print(f"请用微信扫描二维码：{state.directory / 'login.png'}", flush=True)
    if sys.stdout.isatty():
        qr.print_ascii(invert=True)


async def login(client, state, *, show=show_qr, read_code=None, sleep=asyncio.sleep):
    previous = state.account()
    client.base_url = API_BASE
    verify = ""
    code = ""
    refreshes = 0
    deadline = time.monotonic() + 480
    last_status = ""
    try:
        while time.monotonic() < deadline:
            if not code:
                result = await client.request("get_bot_qrcode", {
                    "local_token_list": [previous["token"]] if previous else [],
                }, query={"bot_type": 3}, login=True)
                code = result.get("qrcode")
                if not isinstance(code, str) or not code:
                    raise ProtocolError("微信未返回二维码。")
                show(state, result.get("qrcode_img_content"))
            query = {"qrcode": code}
            if verify:
                query["verify_code"] = verify
            result = await client.request("get_qrcode_status", query=query, login=True, timeout=40)
            status = result.get("status")
            if status == "confirmed":
                account = confirmed_account(result)
                if previous and (account["bot_id"], account["owner"]) != (previous["bot_id"], previous["owner"]):
                    raise ProtocolError("扫码账号与已保存的绑定不同，未覆盖原账号。")
                state.write("account.json", account)
                print(f"扫码绑定成功。API 主机：{urlsplit(account['base_url']).hostname}；凭证已安全保存。", flush=True)
                return account
            if status == "scaned_but_redirect":
                host = result.get("redirect_host")
                if not isinstance(host, str) or "/" in host or ":" in host:
                    raise ProtocolError("扫码重定向主机无效。")
                client.base_url = trusted_url("https://" + host, api=True)
            elif status == "need_verifycode":
                print("微信要求验证码，请在本机终端输入手机显示的数字。", flush=True)
                verify = (read_code() if read_code else getpass.getpass("验证码（输入不回显）：")).strip()
                if not verify.isascii() or not verify.isdigit() or not 1 <= len(verify) <= 12:
                    raise ProtocolError("验证码应为手机显示的数字。")
                continue
            elif status == "expired":
                refreshes += 1
                if refreshes > 2:
                    raise ProtocolError("二维码多次过期，请重新运行。")
                print("二维码过期，正在刷新。", flush=True)
                code, verify = "", ""
                client.base_url = API_BASE
            elif status == "verify_code_blocked":
                raise ProtocolError("验证码尝试次数过多，请稍后重新登录。")
            elif status == "binded_redirect":
                if previous:
                    print("微信报告已有绑定，续用本机保存的凭证。", flush=True)
                    return previous
                raise ProtocolError("微信报告已有绑定，但本机没有凭证；请在微信检查原连接。")
            elif status not in ("wait", "scaned"):
                raise ProtocolError("微信返回未知扫码状态，已停止。")
            if status != last_status and status in ("wait", "scaned"):
                print("等待微信扫码。" if status == "wait" else "已扫码，等待手机确认。", flush=True)
                last_status = status
            if status == "scaned":
                verify = ""
            await sleep(1)
        raise ProtocolError("扫码等待超时，请重新运行。")
    finally:
        (state.directory / "login.png").unlink(missing_ok=True)


def allowed_message(message, account):
    if not isinstance(message, dict):
        return False
    return (message.get("from_user_id") == account["owner"]
            and message.get("from_user_id") != account["bot_id"]
            and message.get("to_user_id") == account["bot_id"]
            and message.get("message_type") == 1 and message.get("message_state") == 2
            and not message.get("group_id"))


def action_for(message):
    items = message.get("item_list") or []
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        return None
    if any(i.get("type") == 1 and not isinstance(i.get("text_item"), dict) for i in items):
        return None
    text = "\n".join(str((i.get("text_item") or {}).get("text", ""))
                     for i in items if i.get("type") == 1)
    text = unicodedata.normalize("NFKC", text).strip().lstrip("/").strip()
    # Referenced text is deliberately not interpreted as a command.
    if text in ("测试", "微信测试", "帮助"):
        return ("text", 0)
    if text == "结束测试":
        return ("stop", 0)
    delay = re.fullmatch(r"延迟\s*(120|300)", text)
    if delay:
        return ("delay", int(delay.group(1)))
    media = [i for i in items if i.get("type") in (2, 4)]
    if len(media) == 1 and len(items) <= 2:
        return ("media", media[0])
    if items and any(i.get("type") != 1 for i in items):
        return ("unsupported", 0)
    return None


class Probe:
    def __init__(self, client, state, account, *, sleep=asyncio.sleep):
        self.client, self.state, self.account = client, state, account
        self.sleep = sleep
        self.started_at_ms = int(time.time() * 1000) - 5000
        self.media = Media(client)
        self.saved = state.read("probe.json", {"cursor": "", "seen": {}})
        if not isinstance(self.saved.get("cursor"), str) or not isinstance(self.saved.get("seen"), dict):
            raise ProtocolError("诊断游标格式无效，已停止。")
        uncertain = sum(v != "sent" for v in self.saved["seen"].values())
        if uncertain:
            print(f"有 {uncertain} 条先前测试未确认完成；保留记录，不会自动重发。", flush=True)

    def save(self):
        self.state.write("probe.json", self.saved)

    async def handle(self, message):
        if not allowed_message(message, self.account):
            print("跳过非绑定者完整私聊消息。", flush=True)
            return None
        created = message.get("create_time_ms")
        if not isinstance(created, int) or isinstance(created, bool) or created < self.started_at_ms:
            print("跳过旧消息或缺少时间戳的消息；请现在重新发送测试请求。", flush=True)
            return None
        action = action_for(message)
        if action is None:
            print("收到绑定者消息，但未匹配测试指令或单附件；正文未记录。", flush=True)
            return None
        identity = message.get("message_id")
        context = message.get("context_token")
        if isinstance(identity, bool) or not isinstance(identity, (int, str)) or str(identity) == "" or not isinstance(context, str) or not context:
            print("跳过测试消息：缺少消息 ID 或回复上下文。", flush=True)
            return None
        identity = str(identity)
        if identity in self.saved["seen"]:
            return None
        if len(self.saved["seen"]) >= 2000:
            raise ProtocolError("已达诊断记录上限；请保留现场，进入正式收件实现后再继续。")
        # Record before any send; interrupted/uncertain requests never auto-replay.
        self.saved["seen"][identity] = "started"
        self.save()
        sequence = 0

        async def send(item):
            nonlocal sequence
            sequence += 1
            digest = hashlib.sha256((self.account["bot_id"] + ":" + identity + ":" + str(sequence)).encode()).hexdigest()[:32]
            result = await self.client.send(self.account["owner"], context, item, "qqchat-g0-" + digest)
            has_id = bool(result.get("message_id"))
            print(f"回复 {sequence}：API 返回成功；服务端消息 ID {'有' if has_id else '未提供'}，请以手机收到为准。", flush=True)

        kind, value = action
        try:
            if kind == "delay":
                print(f"已收测试请求，等待 {value} 秒后回复；期间请勿发新消息。", flush=True)
                started = time.monotonic()
                await self.sleep(value)
                print(f"延迟计时完成：目标 {value} 秒，实际等待 {time.monotonic() - started:.1f} 秒。", flush=True)
                await send(text_item(f"延迟测试：已等待 {value} 秒，使用原消息上下文回复。"))
            elif kind == "media":
                try:
                    data, name, media_kind = await self.media.download(value)
                    uploaded = await self.media.upload(data, name, media_kind, self.account["owner"])
                except SessionExpired:
                    raise
                except ProtocolError as exc:
                    await send(text_item("附件测试失败：" + str(exc)))
                    raise
                digest = hashlib.sha256(data).hexdigest()
                await send(text_item(f"附件测试：收到 {len(data)} 字节，正在回传原内容。\nSHA-256：{digest}"))
                await send(uploaded)
                suffix = Path(name).suffix.lower()
                label = "图片" if media_kind == 2 else (suffix if suffix in (".txt", ".pdf", ".xlsx") else "其他文件")
                print(f"附件往返：{label}，{len(data)} 字节；SHA-256 {digest}", flush=True)
            elif kind == "stop":
                await send(text_item("本次微信通道测试已结束。"))
            elif kind == "unsupported":
                await send(text_item("本次只测试文字、单张图片和单个文件；语音、视频及多附件暂不处理。"))
            else:
                await send(text_item("文字收发成功。\n" + HELP))
        except DeliveryUnknown:
            self.saved["seen"][identity] = "unknown"
            self.save()
            raise
        except (ProtocolError, asyncio.CancelledError):
            self.saved["seen"][identity] = "interrupted_or_failed"
            self.save()
            raise
        self.saved["seen"][identity] = "sent"
        self.save()
        return kind

    async def run(self, duration, max_requests):
        print(HELP, flush=True)
        print(f"本次最多 {max_requests} 个测试请求、{duration} 秒；Ctrl+C 可停止，凭证和游标保留。", flush=True)
        count = 0
        poll_timeout = 40
        async with asyncio.timeout(duration):
            while count < max_requests:
                batch = await self.client.updates(self.saved["cursor"], timeout=poll_timeout)
                messages = batch.get("msgs", [])
                cursor = batch.get("get_updates_buf")
                if not isinstance(messages, list) or (cursor is not None and not isinstance(cursor, str)):
                    raise ProtocolError("微信收件批次结构无效，未更新游标。")
                for message in messages:
                    kind = await self.handle(message)
                    if kind:
                        count += 1
                    if kind == "stop" or count >= max_requests:
                        # Keep the old cursor if this batch still has unprocessed messages.
                        return
                if cursor:
                    self.saved["cursor"] = cursor
                    self.save()
                suggested = batch.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and not isinstance(suggested, bool):
                    poll_timeout = max(5, min(60, suggested / 1000 + 5))
                if not messages:
                    await self.sleep(1)


async def main_async(args):
    state = State(args.state_dir)
    with state.lock():
        account = state.account()
        if args.command == "status":
            print("已保存微信绑定，可运行测试。" if account else "尚未登录，运行脚本即可扫码。")
            return
        connector = aiohttp.TCPConnector(resolver=PublicResolver())
        async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                         cookie_jar=aiohttp.DummyCookieJar()) as session:
            client = WeChatClient(session)
            if account is None or args.command == "login":
                account = await login(client, state)
            if args.command == "login":
                return
            client.base_url, client.token = account["base_url"], account["token"]
            probe = Probe(client, state, account)
            try:
                await probe.run(args.duration, args.max_requests)
            except TimeoutError:
                print("诊断时间到，已停止；未完成的请求不会自动重发。", flush=True)


def main():
    parser = argparse.ArgumentParser(description="iLink 微信最小回路：扫码、受控文字和附件测试，不调用模型。")
    parser.add_argument("command", choices=("run", "login", "status"), nargs="?", default="run")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--duration", type=int, default=1200, help="测试时长（秒，最大 3600）")
    parser.add_argument("--max-requests", type=int, default=12, help="最多测试请求数（最大 50）")
    args = parser.parse_args()
    if not 1 <= args.duration <= 3600 or not 1 <= args.max_requests <= 50:
        parser.error("时长须为 1～3600 秒，请求数须为 1～50")
    os.umask(0o077)
    # SIGTERM follows the same cancellation/finally path as Ctrl+C.
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        asyncio.run(main_async(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("诊断已停止；凭证和游标保留。", flush=True)
    except (ProtocolError, OSError, EOFError) as exc:
        detail = str(exc) if isinstance(exc, ProtocolError) else "本地文件或终端操作失败，请检查状态目录权限和终端。"
        print("诊断停止：" + detail, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
