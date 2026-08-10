from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .config import Settings

LOGGER = logging.getLogger(__name__)
_IPV4 = re.compile(
    r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)"
)
_COMMON_SECRET = re.compile(
    r"(?i)(?:bearer\s+|sk-|api[_-]?key\s*[:=]\s*)[A-Za-z0-9._-]{8,}"
)

class QQAPIError(RuntimeError):
    pass


class AccessTokenManager:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, *, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.monotonic() < self._expires_at - 90:
                return self._token
            async with self._session.post(
                self._settings.qq_token_url,
                json={
                    "appId": self._settings.qq_app_id,
                    "clientSecret": self._settings.qq_app_secret,
                },
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400 or "access_token" not in data:
                    raise QQAPIError(f"获取 QQ access_token 失败：HTTP {response.status} {str(data)[:400]}")
                self._token = str(data["access_token"])
                self._expires_at = time.monotonic() + int(data.get("expires_in", 7200))
                return self._token


class QQClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self.session = session
        self.tokens = AccessTokenManager(settings, session)

    async def auth_headers(self) -> dict[str, str]:
        token = await self.tokens.get()
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.settings.qq_app_id,
            "content-type": "application/json",
        }

    def sanitize_text(self, content: str) -> str:
        protected = {
            str(getattr(self.settings, "qq_app_id", "")),
            str(getattr(self.settings, "qq_app_secret", "")),
            str(getattr(self.settings, "llm_api_key", "")),
            str(getattr(self.settings, "llm_base_url", "")),
            str(getattr(self.settings, "qq_api_base", "")),
            str(getattr(self.settings, "qq_token_url", "")),
            *getattr(self.settings, "owner_user_ids", ()),
        }
        for value in tuple(protected):
            hostname = urlsplit(value).hostname if "://" in value else None
            if hostname:
                protected.add(hostname)
        safe = content
        for value in sorted(protected, key=len, reverse=True):
            if len(value) >= 6:
                safe = re.sub(re.escape(value), "[内部信息已隐藏]", safe, flags=re.I)
        safe = _COMMON_SECRET.sub("[内部信息已隐藏]", safe)
        safe = _IPV4.sub("[内部信息已隐藏]", safe)
        if safe != content:
            LOGGER.warning("群消息发送前发现并隐藏了内部信息")
        return safe

    async def send_group_text(
        self,
        group_id: str,
        content: str,
        message_id: str | None = None,
        *,
        sequence: int = 1,
    ) -> dict:
        url = f"{self.settings.qq_api_base}/v2/groups/{group_id}/messages"
        payload = {
            "msg_type": 0,
            "content": self.sanitize_text(content),
        }
        # msg_id/msg_seq are only valid when replying to an incoming message.
        # Proactive messages (for example scheduled greetings) omit both fields.
        if message_id:
            payload["msg_id"] = message_id
            payload["msg_seq"] = sequence
        async with self.session.post(
            url, headers=await self.auth_headers(), json=payload
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise QQAPIError(f"发送群消息失败：HTTP {response.status} {str(data)[:500]}")
            return data

    async def send_group_file(
        self,
        group_id: str,
        path: Path,
        message_id: str,
        *,
        sequence: int = 1,
    ) -> dict:
        """Upload a local file through QQ's official multipart API, then reply with it."""
        file_type = _file_type(path)
        metadata = await asyncio.to_thread(_file_metadata, path)
        prepare_url = f"{self.settings.qq_api_base}/v2/groups/{group_id}/upload_prepare"
        prepare_payload = {
            "file_type": file_type,
            "file_size": str(metadata["size"]),
            "file_name": path.name,
            "md5": metadata["md5"],
            "sha1": metadata["sha1"],
            "md5_10m": metadata["md5_10m"],
        }
        async with self.session.post(
            prepare_url, headers=await self.auth_headers(), json=prepare_payload
        ) as response:
            prepared = await response.json(content_type=None)
            if response.status >= 400 or "upload_id" not in prepared:
                raise QQAPIError(f"准备上传文件失败：HTTP {response.status} {str(prepared)[:500]}")

        upload_id = str(prepared["upload_id"])
        default_block_size = int(prepared.get("block_size") or 5 * 1024 * 1024)
        parts = sorted(prepared.get("parts") or [], key=lambda item: int(item.get("index", 0)))
        if not parts:
            raise QQAPIError("QQ 文件预上传响应中没有分片地址")

        finish_url = f"{self.settings.qq_api_base}/v2/groups/{group_id}/upload_part_finish"
        for position, part in enumerate(parts):
            index = int(part.get("index", 0))
            block_size = int(part.get("block_size") or default_block_size)
            block = await asyncio.to_thread(
                _read_block, path, position * default_block_size, block_size
            )
            if not block and metadata["size"] > 0:
                raise QQAPIError(f"读取文件分片 {index} 失败")
            presigned_url = str(part.get("presigned_url", ""))
            async with self.session.put(presigned_url, data=block) as response:
                if response.status >= 400:
                    detail = (await response.text())[:300]
                    raise QQAPIError(f"上传文件分片 {index} 失败：HTTP {response.status} {detail}")
            finish_payload = {
                "upload_id": upload_id,
                "part_index": index,
                "block_size": str(len(block)),
                "md5": hashlib.md5(block).hexdigest(),  # noqa: S324 - required by QQ API
            }
            async with self.session.post(
                finish_url, headers=await self.auth_headers(), json=finish_payload
            ) as response:
                if response.status >= 400:
                    detail = (await response.text())[:300]
                    raise QQAPIError(f"确认文件分片 {index} 失败：HTTP {response.status} {detail}")

        merge_url = f"{self.settings.qq_api_base}/v2/groups/{group_id}/files"
        merge_payload = {
            "file_type": file_type,
            "srv_send_msg": False,
            "file_name": path.name,
            "upload_id": upload_id,
        }
        async with self.session.post(
            merge_url, headers=await self.auth_headers(), json=merge_payload
        ) as response:
            merged = await response.json(content_type=None)
            if response.status >= 400 or "file_info" not in merged:
                raise QQAPIError(f"合并上传文件失败：HTTP {response.status} {str(merged)[:500]}")

        send_url = f"{self.settings.qq_api_base}/v2/groups/{group_id}/messages"
        send_payload = {
            "msg_type": 7,
            "msg_id": message_id,
            "msg_seq": sequence,
            "media": {"file_info": str(merged["file_info"])},
        }
        async with self.session.post(
            send_url, headers=await self.auth_headers(), json=send_payload
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise QQAPIError(f"发送群文件失败：HTTP {response.status} {str(data)[:500]}")
            return data

    async def download_attachment(self, url: str, target: Path, max_bytes: int) -> int:
        if not url.startswith("https://"):
            raise QQAPIError("QQ 附件下载地址不是 HTTPS")
        temporary = target.with_name(f".{target.name}.part")
        written = 0
        try:
            async with self.session.get(url) as response:
                if response.status >= 400:
                    raise QQAPIError(f"下载 QQ 附件失败：HTTP {response.status}")
                declared = response.content_length
                if declared is not None and declared > max_bytes:
                    raise QQAPIError("QQ 附件超过工作区大小限制")
                with temporary.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            raise QQAPIError("QQ 附件超过工作区大小限制")
                        handle.write(chunk)
            temporary.replace(target)
            return written
        finally:
            if temporary.exists():
                temporary.unlink()


def _file_type(path: Path) -> int:
    suffix = path.suffix.casefold()
    if suffix in {".png", ".jpg", ".jpeg"}:
        return 1
    if suffix == ".mp4":
        return 2
    if suffix == ".silk":
        return 3
    return 4


def _file_metadata(path: Path) -> dict[str, str | int]:
    md5 = hashlib.md5()  # noqa: S324 - checksum required by QQ API
    sha1 = hashlib.sha1()  # noqa: S324 - checksum required by QQ API
    first = bytearray()
    first_limit = 10_002_432
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            md5.update(chunk)
            sha1.update(chunk)
            if len(first) < first_limit:
                first.extend(chunk[: first_limit - len(first)])
    return {
        "size": size,
        "md5": md5.hexdigest(),
        "sha1": sha1.hexdigest(),
        "md5_10m": hashlib.md5(first).hexdigest(),  # noqa: S324 - required by QQ API
    }


def _read_block(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size)
