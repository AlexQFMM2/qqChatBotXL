from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.qq import QQClient, _file_type, _read_block


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, content_type=None):
        return {"id": "sent"}


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeResponse()


class FileBlockTests(unittest.TestCase):
    def test_reads_blocks_by_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.bin"
            path.write_bytes(b"abcdefghij")
            self.assertEqual(_read_block(path, 0, 4), b"abcd")
            self.assertEqual(_read_block(path, 4, 4), b"efgh")
            self.assertEqual(_read_block(path, 8, 4), b"ij")

    def test_silk_uses_qq_audio_file_type(self) -> None:
        self.assertEqual(_file_type(Path("reply.silk")), 3)


class GroupTextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session = FakeSession()
        settings = SimpleNamespace(
            qq_api_base="https://api.example.test",
            qq_token_url="https://api.example.test/token",
            qq_app_id="app-id",
            qq_app_secret="secret",
            owner_user_ids=frozenset({"owner-openid-value"}),
        )
        self.client = QQClient(settings, self.session)  # type: ignore[arg-type]
        self.client.auth_headers = AsyncMock(return_value={"Authorization": "test"})

    async def test_proactive_message_omits_reply_fields(self) -> None:
        await self.client.send_group_text("group", "早安")
        payload = self.session.calls[-1]["json"]
        self.assertEqual(payload, {"msg_type": 0, "content": "早安"})

    async def test_reply_message_keeps_message_id_and_sequence(self) -> None:
        await self.client.send_group_text("group", "回复", "message", sequence=3)
        payload = self.session.calls[-1]["json"]
        self.assertEqual(payload["msg_id"], "message")
        self.assertEqual(payload["msg_seq"], 3)

    async def test_every_outgoing_text_path_redacts_openid(self) -> None:
        await self.client.send_group_text("group", "老师 owner-openid-value")
        payload = self.session.calls[-1]["json"]
        self.assertNotIn("owner-openid-value", payload["content"])


if __name__ == "__main__":
    unittest.main()
