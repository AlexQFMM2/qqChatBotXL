from __future__ import annotations

import asyncio
import binascii
import json
import logging
import time
from collections.abc import Awaitable, Callable

from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

LOGGER = logging.getLogger(__name__)
MAX_CALLBACK_BODY = 1024 * 1024


def _secret_seed(secret: str) -> bytes:
    value = secret.encode("utf-8")
    if not value:
        raise ValueError("QQ_APP_SECRET 不能为空")
    seed = value
    while len(seed) < 32:
        seed += seed
    return seed[:32]


def private_key_from_secret(secret: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_secret_seed(secret))


def validation_signature(secret: str, event_ts: str, plain_token: str) -> str:
    message = f"{event_ts}{plain_token}".encode("utf-8")
    return private_key_from_secret(secret).sign(message).hex()


def verify_callback_signature(
    secret: str,
    timestamp: str,
    raw_body: bytes,
    signature_hex: str,
) -> bool:
    if not timestamp or not signature_hex:
        return False
    try:
        signature = bytes.fromhex(signature_hex)
    except (ValueError, binascii.Error):
        return False
    if len(signature) != 64 or signature[63] & 0b11100000:
        return False
    public_key = private_key_from_secret(secret).public_key()
    try:
        public_key.verify(signature, timestamp.encode("utf-8") + raw_body)
        return True
    except InvalidSignature:
        return False


class WebhookReceiver:
    def __init__(
        self,
        app_id: str,
        secret: str,
        on_event: Callable[[str, dict], Awaitable[None]],
        *,
        workers: int = 2,
        queue_size: int = 500,
    ) -> None:
        self._app_id = app_id
        self._secret = secret
        self._on_event = on_event
        self._worker_count = workers
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=queue_size)
        self._tasks: list[asyncio.Task] = []
        self.ready = False
        self.last_event_at: float | None = None
        self.accepted_events = 0
        self.rejected_requests = 0

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"webhook-worker-{index}")
            for index in range(self._worker_count)
        ]
        self.ready = True

    async def close(self) -> None:
        self.ready = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def handle(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Bot-Appid", "") != self._app_id:
            self.rejected_requests += 1
            raise web.HTTPUnauthorized(text="invalid app id")
        if request.content_length is not None and request.content_length > MAX_CALLBACK_BODY:
            self.rejected_requests += 1
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_CALLBACK_BODY, actual_size=request.content_length
            )

        raw_body = await request.read()
        if len(raw_body) > MAX_CALLBACK_BODY:
            self.rejected_requests += 1
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_CALLBACK_BODY, actual_size=len(raw_body)
            )
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.rejected_requests += 1
            raise web.HTTPBadRequest(text="invalid json")
        if not isinstance(payload, dict):
            self.rejected_requests += 1
            raise web.HTTPBadRequest(text="invalid payload")

        opcode = payload.get("op")
        if opcode == 13:
            return self._validation_response(payload)
        if opcode != 0:
            self.rejected_requests += 1
            raise web.HTTPBadRequest(text="unsupported opcode")

        timestamp = request.headers.get("X-Signature-Timestamp", "")
        signature = request.headers.get("X-Signature-Ed25519", "")
        if not verify_callback_signature(self._secret, timestamp, raw_body, signature):
            self.rejected_requests += 1
            raise web.HTTPUnauthorized(text="invalid signature")

        event_type = str(payload.get("t", ""))
        data = payload.get("d")
        if not event_type or not isinstance(data, dict):
            self.rejected_requests += 1
            raise web.HTTPBadRequest(text="invalid event")
        try:
            self._queue.put_nowait((event_type, data))
        except asyncio.QueueFull:
            raise web.HTTPServiceUnavailable(text="event queue full")

        self.last_event_at = time.time()
        self.accepted_events += 1
        return web.json_response({"op": 12})

    def _validation_response(self, payload: dict) -> web.Response:
        data = payload.get("d")
        if not isinstance(data, dict):
            self.rejected_requests += 1
            raise web.HTTPBadRequest(text="invalid validation payload")
        plain_token = str(data.get("plain_token", ""))
        event_ts = str(data.get("event_ts", ""))
        if not plain_token or not event_ts:
            self.rejected_requests += 1
            raise web.HTTPBadRequest(text="invalid validation fields")
        return web.json_response(
            {
                "plain_token": plain_token,
                "signature": validation_signature(self._secret, event_ts, plain_token),
            }
        )

    async def _worker(self, index: int) -> None:
        while True:
            event_type, data = await self._queue.get()
            try:
                await self._on_event(event_type, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Webhook worker %s 处理事件失败：%s", index, event_type)
            finally:
                self._queue.task_done()
