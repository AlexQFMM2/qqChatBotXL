from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
import secrets
import warnings
from urllib.parse import urlencode

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
from yarl import URL

from .client import CDN_BASE, ProtocolError, limited_body, trusted_url

MAX_MEDIA = 5 * 1024 * 1024  # Diagnostic limit, not a claim about the service.


def decode_key(encoded: str) -> bytes:
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) == 32:
            decoded = bytes.fromhex(decoded.decode("ascii"))
        if len(decoded) != 16:
            raise ValueError
        return decoded
    except (ValueError, UnicodeError, TypeError):
        raise ProtocolError("媒体密钥格式无效。") from None


def crypt(data: bytes, key: bytes, *, decrypt=False) -> bytes:
    try:
        if len(key) != 16:
            raise ValueError
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        if decrypt:
            transform = cipher.decryptor()
            padded = transform.update(data) + transform.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return unpadder.update(padded) + unpadder.finalize()
        padder = padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        transform = cipher.encryptor()
        return transform.update(padded) + transform.finalize()
    except ValueError:
        raise ProtocolError("媒体加解密失败（密钥、密文或填充无效）。") from None


def safe_filename(value):
    name = re.split(r"[/\\]", str(value or "sample.bin"))[-1]
    name = re.sub(r"[^\w.()\- ]", "_", name).strip(" .")[:100]
    return name or "sample.bin"


def verify_image(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                if picture.width * picture.height > 20_000_000:
                    raise ValueError
                if picture.format not in ("PNG", "JPEG", "GIF", "WEBP"):
                    raise ValueError
                picture.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ProtocolError("图片无效、格式不支持或像素超过诊断上限。") from None


class Media:
    def __init__(self, client):
        self.client = client

    async def transfer(self, method, url, data=None):
        url = trusted_url(url)
        try:
            # No Bot auth on CDN requests. This session has no default credentials.
            async with self.client.session.request(
                method, URL(url, encoded=True), data=data, headers={"Content-Type": "application/octet-stream"},
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    raise ProtocolError(f"微信 CDN HTTP {response.status}；未跟随重定向。")
                if method == "POST":
                    param = response.headers.get("x-encrypted-param")
                    if not param:
                        raise ProtocolError("CDN 上传缺少确认参数。")
                    return param
                return await limited_body(response, MAX_MEDIA + 16)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ProtocolError("微信 CDN 网络失败，请重新发起附件测试。") from None

    async def download(self, item):
        kind = item.get("type")
        if kind not in (2, 4):
            raise ProtocolError("诊断只支持图片和文件。")
        body = item.get("image_item" if kind == 2 else "file_item") or {}
        if not isinstance(body, dict):
            raise ProtocolError("媒体结构无效。")
        media = body.get("media") or {}
        if not isinstance(body, dict) or not isinstance(media, dict):
            raise ProtocolError("媒体结构无效。")
        if media.get("encrypt_type") not in (None, 0, 1):
            raise ProtocolError("媒体加密类型暂不支持。")
        full = media.get("full_url")
        param = media.get("encrypt_query_param")
        if not full and not param:
            raise ProtocolError("媒体缺少下载地址。")
        url = full or CDN_BASE + "/download?" + urlencode({"encrypted_query_param": param})
        key = None
        if kind == 2 and body.get("aeskey"):
            try:
                key = bytes.fromhex(body["aeskey"])
            except (ValueError, TypeError):
                raise ProtocolError("图片密钥格式无效。") from None
            if len(key) != 16:
                raise ProtocolError("图片密钥长度无效。")
        elif media.get("aes_key"):
            key = decode_key(media["aes_key"])
        elif kind == 4:
            raise ProtocolError("文件缺少解密密钥。")
        raw = await self.transfer("GET", url)
        data = crypt(raw, key, decrypt=True) if key is not None else raw
        if len(data) > MAX_MEDIA:
            raise ProtocolError("附件超过 5 MiB 诊断上限。")
        if body.get("len") is not None and str(len(data)) != str(body["len"]):
            raise ProtocolError("文件长度校验失败。")
        if body.get("md5") and hashlib.md5(data).hexdigest().lower() != str(body["md5"]).lower():
            raise ProtocolError("文件 MD5 校验失败。")
        if kind == 2:
            verify_image(data)
        return data, safe_filename(body.get("file_name")), kind

    async def upload(self, data, filename, kind, owner):
        if len(data) > MAX_MEDIA:
            raise ProtocolError("附件超过 5 MiB 诊断上限。")
        if kind not in (2, 4):
            raise ProtocolError("诊断只支持图片和文件。")
        if kind == 2:
            verify_image(data)
        key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
        encrypted = crypt(data, key)
        result = await self.client.request("getuploadurl", {
            "filekey": filekey, "media_type": 1 if kind == 2 else 3,
            "to_user_id": owner, "rawsize": len(data),
            "rawfilemd5": hashlib.md5(data).hexdigest(), "filesize": len(encrypted),
            "no_need_thumb": True, "aeskey": key.hex(),
        })
        url = result.get("upload_full_url")
        if not url:
            if not result.get("upload_param"):
                raise ProtocolError("微信未返回上传地址。")
            url = CDN_BASE + "/upload?" + urlencode({
                "encrypted_query_param": result["upload_param"], "filekey": filekey})
        param = await self.transfer("POST", url, encrypted)
        media = {"encrypt_query_param": param,
                 "aes_key": base64.b64encode(key.hex().encode()).decode(), "encrypt_type": 1}
        if kind == 2:
            return {"type": 2, "image_item": {"media": media, "mid_size": len(encrypted)}}
        return {"type": 4, "file_item": {
            "media": media, "file_name": safe_filename(filename), "len": str(len(data)),
        }}
