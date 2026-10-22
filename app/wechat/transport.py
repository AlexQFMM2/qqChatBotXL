from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import aiohttp

from ..channel import IncomingMessage, TransportError
from ..domain import AttachmentRef
from .client import ProtocolError, SessionExpired, text_item
from .media import MAX_MEDIA, Media, safe_filename
from .state import atomic_write


def conversation_id(account):
    digest = hashlib.sha256((account['bot_id'] + ':' + account['owner']).encode()).hexdigest()[:32]
    return 'wechat:' + digest


class WeChatTransport:
    def __init__(self, client, inbox, account):
        self.client, self.inbox, self.account = client, inbox, account
        self.media = Media(client)
        self.conversation_id = conversation_id(account)
        self.active = None
        self.counter = 0
        self.uncertain = False
        self.expired = False

    def bind(self, payload, *, recovery=False):
        from .__main__ import allowed_message
        if not allowed_message(payload, self.account):
            raise TransportError('拒绝非绑定者私聊消息。')
        self.active, self.counter, self.uncertain = payload, 0, False
        self.recovery = recovery

    def normalize(self, owner_title):
        payload = self.active
        items = payload.get('item_list') or []
        if payload.get('input_rejected') or not isinstance(items, list) or len(items) > 10:
            return None, '这条消息过大，请拆成较短的文字或少量附件再发。'
        texts, attachments, unsupported = [], [], False
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                unsupported = True
                continue
            kind = item.get('type')
            body = item.get({1: 'text_item', 2: 'image_item', 4: 'file_item'}.get(kind, ''))
            if not isinstance(body, dict):
                unsupported = True
            elif kind == 1 and isinstance(body.get('text'), str):
                texts.append(body['text'])
            elif kind in (2, 4):
                name = f'image_{index+1}.jpg' if kind == 2 else safe_filename(body.get('file_name'))
                attachments.append(AttachmentRef({
                    'url': f'wechat-attachment:{index}', 'filename': name,
                    'content_type': 'image' if kind == 2 else 'application/octet-stream',
                }, 'current_user'))
            else:
                unsupported = True
        content = '\n'.join(texts).strip()
        if len(content) > 20000:
            return None, '文字过长，请拆成每条不超过 20000 字后再发。'
        if unsupported:
            return None, '这条消息含暂不支持的语音、视频或其他类型，请改发文字、图片或文件。'
        if not content and not attachments:
            return None, '没有收到可处理的文字或附件，请重新发送。'
        command = content or ('请看这张图片。' if all(a.data['content_type']=='image' for a in attachments)
                              else '请保存这些附件，并告诉我保存结果。')
        return IncomingMessage(
            channel='wechat', conversation_id=self.conversation_id,
            message_id=str(payload['message_id']), user_id=self.account['owner'],
            username=owner_title, role='owner', content=command,
            command_content=command, attachments=tuple(attachments),
        ), None

    def _check_destination(self, conversation, message_id):
        if (not self.active or conversation != self.conversation_id
                or str(message_id) != str(self.active['message_id'])):
            raise TransportError('没有本条入站消息的有效回复上下文，未发送。')
        if self.uncertain or self.expired:
            raise TransportError('本条消息已有未确认发送；已停止后续发送，请在手机核对。')

    async def _send(self, conversation, message_id, item):
        self._check_destination(conversation, message_id)
        self.counter += 1
        digest = hashlib.sha256((self.account['bot_id'] + ':' + str(message_id)
                                 + (':recovery:' if self.recovery else ':reply:')
                                 + str(self.counter)).encode()).hexdigest()[:40]
        identity = 'qqchat-wx-' + digest
        status, response = self.inbox.begin_send(identity, str(message_id))
        if status == 'sent':
            return response
        if status != 'new':
            self.uncertain = True
            raise TransportError('上次发送结果不明，不会自动重发。')
        try:
            result = await self.client.send(self.account['owner'], self.active['context_token'], item, identity)
        except SessionExpired:
            self.expired = True
            self.inbox.end_send(identity, 'unknown')
            raise TransportError('微信登录已失效，请在本机重新登录。') from None
        except (ProtocolError, asyncio.CancelledError):
            self.uncertain = True
            self.inbox.end_send(identity, 'unknown')
            raise
        response = {'id': str(result.get('message_id') or '')}
        try:
            self.inbox.end_send(identity, 'sent', response)
        except Exception:
            # The server accepted this send, but we cannot commit that fact.
            # Keep the existing sending journal for recovery and prohibit retries.
            self.uncertain = True
            raise TransportError('回复已提交，但本机确认写盘失败；请核对手机，已停止后续操作。') from None
        return response

    async def send_text(self, conversation, content, message_id=None, *, sequence=1):
        # Keep every API send below 4000 UTF-8 bytes, without splitting a codepoint.
        remaining = content[:12000]
        result = {}
        while remaining:
            chunk = remaining.encode()[:3900].decode('utf-8', errors='ignore')
            try:
                result = await self._send(conversation, message_id, text_item(chunk))
            except ProtocolError:
                raise TransportError('微信回复未确认成功，请核对手机；不会自动重发。') from None
            remaining = remaining[len(chunk):]
        return result

    async def send_file(self, conversation, path: Path, message_id, *, sequence=1):
        self._check_destination(conversation, message_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MEDIA:
            raise TransportError('文件不存在、不是普通文件或超过当前微信 5 MiB 上限。')
        def read_bounded():
            with path.open('rb') as stream:
                return stream.read(MAX_MEDIA + 1)
        data = await asyncio.to_thread(read_bounded)
        kind = 2 if path.suffix.lower() in {'.png','.jpg','.jpeg','.gif','.webp'} else 4
        try:
            item = await self.media.upload(data, path.name, kind, self.account['owner'])
            return await self._send(conversation, message_id, item)
        except SessionExpired:
            self.expired = True
            raise TransportError('微信登录已失效。') from None
        except ProtocolError:
            raise TransportError('微信附件传输未成功确认；请核对手机和工作区。') from None

    async def download_attachment(self, reference, target, max_bytes):
        try:
            if reference.startswith('wechat-attachment:'):
                index = int(reference.split(':', 1)[1])
                if not self.active or index < 0:
                    raise ValueError
                data, _, _ = await self.media.download(self.active['item_list'][index])
                if len(data) > min(MAX_MEDIA, max_bytes):
                    raise TransportError('附件超过当前微信大小限制或工作区剩余配额。')
                atomic_write(target, data)
                return len(data)
            # Image-generation artifacts are not inbound WeChat CDN references.
            from ..bot import _allowed_generated_image_url
            if not _allowed_generated_image_url(reference):
                raise TransportError('拒绝未授权的附件下载地址。')
            from .client import limited_body
            async with self.client.session.get(reference, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    raise TransportError('生成图片下载失败。')
                data = await limited_body(response, min(MAX_MEDIA, max_bytes))
            atomic_write(target, data)
            return len(data)
        except SessionExpired:
            self.expired = True
            raise TransportError('微信登录已失效。') from None
        except (ProtocolError, aiohttp.ClientError, TimeoutError, ValueError, IndexError, KeyError):
            raise TransportError('附件下载或完整性校验失败，请重新发送。') from None
