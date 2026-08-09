from __future__ import annotations

import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.webhook import (
    WebhookReceiver,
    private_key_from_secret,
    validation_signature,
    verify_callback_signature,
)


class SignatureTests(unittest.TestCase):
    def test_official_validation_example(self) -> None:
        signature = validation_signature(
            "DG5g3B4j9X2KOErG", "1725442341", "Arq0D5A61EgUu4OxUvOp"
        )
        self.assertEqual(
            signature,
            "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01"
            "bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706",
        )

    def test_event_signature_and_tamper_detection(self) -> None:
        secret = "naOC0ocQE3shWLAfffVLB1rhYPG7"
        timestamp = "1725442341"
        body = b'{"op":0,"d":{},"t":"GATEWAY_EVENT_NAME"}'
        signature = private_key_from_secret(secret).sign(
            timestamp.encode() + body
        ).hex()
        self.assertTrue(verify_callback_signature(secret, timestamp, body, signature))
        self.assertFalse(
            verify_callback_signature(secret, timestamp, body + b" ", signature)
        )
        self.assertFalse(verify_callback_signature(secret, timestamp, body, "invalid"))


class WebhookReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.block_event = asyncio.Event()

        async def on_event(event_type: str, data: dict) -> None:
            self.events.append((event_type, data))
            await self.block_event.wait()

        self.receiver = WebhookReceiver(
            "1905383407", "test-secret", on_event, workers=1, queue_size=10
        )
        await self.receiver.start()
        app = web.Application()
        app.router.add_post("/qq/webhook", self.receiver.handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        self.block_event.set()
        await self.receiver.close()
        await self.client.close()

    async def test_validation_challenge(self) -> None:
        payload = {
            "op": 13,
            "d": {"plain_token": "plain-value", "event_ts": "1725442341"},
        }
        response = await self.client.post(
            "/qq/webhook",
            json=payload,
            headers={"X-Bot-Appid": "1905383407"},
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["plain_token"], "plain-value")
        self.assertEqual(
            body["signature"],
            validation_signature("test-secret", "1725442341", "plain-value"),
        )

    async def test_signed_event_is_acked_before_worker_finishes(self) -> None:
        payload = {
            "id": "event-1",
            "op": 0,
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": "message-1", "group_openid": "group-1"},
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = "1725442341"
        signature = private_key_from_secret("test-secret").sign(
            timestamp.encode() + raw
        ).hex()
        response = await asyncio.wait_for(
            self.client.post(
                "/qq/webhook",
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Bot-Appid": "1905383407",
                    "X-Signature-Timestamp": timestamp,
                    "X-Signature-Ed25519": signature,
                },
            ),
            timeout=1,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"op": 12})
        for _ in range(20):
            if self.events:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(
            self.events,
            [("GROUP_AT_MESSAGE_CREATE", payload["d"])],
        )

    async def test_invalid_signature_is_rejected(self) -> None:
        response = await self.client.post(
            "/qq/webhook",
            json={"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {}},
            headers={
                "X-Bot-Appid": "1905383407",
                "X-Signature-Timestamp": "1725442341",
                "X-Signature-Ed25519": "00" * 64,
            },
        )
        self.assertEqual(response.status, 401)

    async def test_wrong_app_id_is_rejected(self) -> None:
        response = await self.client.post(
            "/qq/webhook",
            json={"op": 13, "d": {"plain_token": "x", "event_ts": "1"}},
            headers={"X-Bot-Appid": "wrong"},
        )
        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
