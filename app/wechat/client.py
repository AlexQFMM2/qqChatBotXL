from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import secrets
import socket
from urllib.parse import urlencode, urlsplit

import aiohttp

API_BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
PROTOCOL_VERSION = "2.4.10"


class ProtocolError(RuntimeError):
    """Only sanitized, locally constructed messages may be used here."""


class SessionExpired(ProtocolError):
    pass


class DeliveryUnknown(ProtocolError):
    pass


def trusted_url(value: str, *, api: bool = False) -> str:
    """Allow HTTPS Weixin hosts only; do not follow HTTP redirects."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        valid = (parsed.scheme == "https" and host.endswith(".weixin.qq.com")
                 and parsed.port in (None, 443) and not parsed.username
                 and not parsed.password and not parsed.fragment)
        if api:
            valid = valid and parsed.path in ("", "/") and not parsed.query
        if not valid or any(c.isspace() for c in value) or "\\" in value:
            raise ValueError
    except (ValueError, TypeError):
        raise ProtocolError("拒绝非受信的微信 HTTPS 地址；未发送凭证。") from None
    return value.rstrip("/") if api else value


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Check resolved addresses as well as the hostname before connecting."""

    def __init__(self):
        self.delegate = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self.delegate.resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(r["host"]).is_global for r in records):
            raise OSError("微信地址解析到了非公网地址")
        return records

    async def close(self):
        await self.delegate.close()


async def limited_body(response, limit: int) -> bytes:
    if response.content_length is not None and response.content_length > limit:
        raise ProtocolError("响应超过诊断大小上限。")
    result = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        if len(result) + len(chunk) > limit:
            raise ProtocolError("响应超过诊断大小上限。")
        result.extend(chunk)
    return bytes(result)


class WeChatClient:
    def __init__(self, session, *, base_url=API_BASE, token=""):
        # The session must have no default Authorization header or auth object.
        self.session = session
        self.base_url = trusted_url(base_url, api=True)
        self.token = token

    async def request(self, endpoint, payload=None, *, query=None, login=False,
                      timeout=20):
        base = trusted_url(self.base_url, api=True)
        url = f"{base}/ilink/bot/{endpoint}"
        if query:
            url += "?" + urlencode(query)
        method = "GET" if endpoint == "get_qrcode_status" else "POST"
        headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str(0x02040A)}
        kwargs = {}
        if method == "POST":
            headers.update({
                "Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
                "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
            })
            body = dict(payload or {})
            if not login:
                if not self.token:
                    raise ProtocolError("尚未登录，请先扫码。")
                headers["Authorization"] = "Bearer " + self.token
                body["base_info"] = {"channel_version": PROTOCOL_VERSION,
                                     "bot_agent": "QQChat-G0/0.1"}
            kwargs["json"] = body
        try:
            async with self.session.request(
                method, url, headers=headers, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout), **kwargs,
            ) as response:
                if response.status in (401, 403):
                    raise SessionExpired("微信鉴权被拒绝，请重新登录；轮询已停止。")
                if not 200 <= response.status < 300:
                    raise ProtocolError(f"微信 API HTTP {response.status}；未记录响应正文。")
                raw = await limited_body(response, 2 * 1024 * 1024)
                try:
                    data = json.loads(raw)  # Python integers preserve 64-bit message IDs.
                except (ValueError, UnicodeError):
                    raise ProtocolError("微信 API 返回的 JSON 无效。") from None
                if not isinstance(data, dict):
                    raise ProtocolError("微信 API 响应不是对象。")
                for key in ("ret", "errcode"):
                    code = data.get(key, 0)
                    if code in (-14, "-14"):
                        raise SessionExpired("微信登录已失效（-14），请重新登录；轮询已停止。")
                    if code not in (0, "0", None):
                        safe_code = str(code) if isinstance(code, int) else "非零"
                        raise ProtocolError(f"微信 API 业务失败：{key}={safe_code}。")
                return data
        except asyncio.TimeoutError:
            if endpoint == "getupdates":
                return {"msgs": []}
            if endpoint == "get_qrcode_status":
                return {"status": "wait"}
            if endpoint == "sendmessage":
                raise DeliveryUnknown("回复结果不明（网络超时）；不会自动重发，请在手机核对。") from None
            raise ProtocolError("微信请求超时。") from None
        except aiohttp.ClientError:
            if endpoint == "sendmessage":
                raise DeliveryUnknown("回复结果不明（网络中断或超时）；不会自动重发，请在手机核对。") from None
            if endpoint == "getupdates":
                raise ProtocolError("收件网络超时或连接失败，可重新启动诊断。") from None
            raise ProtocolError("微信请求网络超时或连接失败。") from None

    async def updates(self, cursor, *, timeout=40):
        return await self.request("getupdates", {"get_updates_buf": cursor}, timeout=timeout)

    async def send(self, owner, context, item, client_id):
        if not owner or not context:
            raise ProtocolError("缺少可信收件人或回复上下文，未发送。")
        try:
            return await self.request("sendmessage", {"msg": {
                "from_user_id": "", "to_user_id": owner, "client_id": client_id,
                "message_type": 2, "message_state": 2, "context_token": context,
                "item_list": [item],
            }})
        except SessionExpired:
            raise
        except DeliveryUnknown:
            raise
        except ProtocolError:
            # A malformed response or server error may follow an accepted send.
            raise DeliveryUnknown("回复未确认成功；不会自动重发，请在手机核对并重新发起测试。") from None


def text_item(text):
    return {"type": 1, "text_item": {"text": text}}
