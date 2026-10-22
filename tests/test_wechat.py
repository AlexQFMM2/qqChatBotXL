from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from PIL import Image

from app.wechat.client import (API_BASE, DeliveryUnknown, ProtocolError, PublicResolver,
                               SessionExpired, WeChatClient, text_item, trusted_url)
from app.wechat.media import MAX_MEDIA, Media, crypt, decode_key, safe_filename
from app.wechat.state import State, confirmed_account
from app.wechat.__main__ import Probe, action_for, allowed_message, login, show_qr

ACCOUNT = {"token": "private-bot-token", "bot_id": "test-bot", "owner": "trusted-owner",
           "base_url": API_BASE}
CONFIRMED = {"status": "confirmed", "bot_token": ACCOUNT["token"],
             "ilink_bot_id": ACCOUNT["bot_id"], "ilink_user_id": ACCOUNT["owner"],
             "baseurl": API_BASE}


def message(text="测试", **changes):
    return {"message_id": 9223372036854775807, "from_user_id": ACCOUNT["owner"],
            "to_user_id": ACCOUNT["bot_id"], "message_type": 1, "message_state": 2,
            "context_token": "private-context", "create_time_ms": int(time.time() * 1000),
            "item_list": [text_item(text)], **changes}


class Body:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, size):
        for start in range(0, len(self.raw), size):
            yield self.raw[start:start + size]


class Response:
    def __init__(self, data=None, *, raw=None, status=200, headers=None, length=True):
        raw = raw if raw is not None else json.dumps(data or {}).encode()
        self.status = status
        self.content = Body(raw)
        self.content_length = len(raw) if length else None
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class APIProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_metadata_and_lossless_id(self):
        session = Session(Response({"ret": 0, "msgs": [message()]}))
        client = WeChatClient(session, token=ACCOUNT["token"])
        result = await client.updates("cursor")
        self.assertEqual(result["msgs"][0]["message_id"], 9223372036854775807)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, API_BASE + "/ilink/bot/getupdates")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer private-bot-token")
        self.assertEqual(kwargs["headers"]["iLink-App-ClientVersion"], str(0x02040A))
        self.assertTrue(base64.b64decode(kwargs["headers"]["X-WECHAT-UIN"]).isdigit())
        self.assertEqual(kwargs["json"], {"get_updates_buf": "cursor", "base_info": {
            "channel_version": "2.4.10", "bot_agent": "QQChat-G0/0.1"}})
        self.assertFalse(kwargs["allow_redirects"])

    async def test_login_post_and_get_have_no_bot_auth(self):
        session = Session(Response({}), Response({"status": "wait"}))
        client = WeChatClient(session, token=ACCOUNT["token"])
        await client.request("get_bot_qrcode", {"local_token_list": []}, login=True)
        await client.request("get_qrcode_status", query={"qrcode": "a+&b", "verify_code": "123"}, login=True)
        for _, _, kwargs in session.calls:
            self.assertNotIn("Authorization", kwargs["headers"])
            self.assertNotIn("base_info", kwargs.get("json", {}))
        self.assertNotIn("AuthorizationType", session.calls[1][2]["headers"])
        self.assertIn("qrcode=a%2B%26b", session.calls[1][1])

    async def test_business_http_and_malformed_errors_are_redacted(self):
        for response in (Response({"ret": 7, "errmsg": "private-bot-token"}),
                         Response({"errcode": 1}), Response(status=302),
                         Response(status=500, raw=b"private-bot-token"),
                         Response(raw=b"private-bot-token"), Response(raw=b"[]")):
            with self.subTest(response=response):
                with self.assertRaises(ProtocolError) as raised:
                    await WeChatClient(Session(response), token="secret").updates("")
                self.assertNotIn("private-bot-token", str(raised.exception))

    async def test_expired_credentials_stop(self):
        for response in (Response({"ret": -14}), Response({"errcode": -14}), Response(status=401)):
            with self.assertRaises(SessionExpired):
                await WeChatClient(Session(response), token="secret").updates("")

    async def test_poll_timeout_is_empty_but_send_timeout_is_unknown(self):
        client = WeChatClient(Session(TimeoutError(), TimeoutError()), token="secret")
        self.assertEqual(await client.updates("saved"), {"msgs": []})
        with self.assertRaises(DeliveryUnknown):
            await client.send("owner", "context", text_item("reply"), "id")
        self.assertEqual(len(client.session.calls), 2)

    async def test_cancellation_propagates(self):
        with self.assertRaises(asyncio.CancelledError):
            await WeChatClient(Session(asyncio.CancelledError()), token="secret").updates("")

    async def test_send_has_context_and_stable_id_no_retry(self):
        session = Session(Response({"ret": 0}))
        await WeChatClient(session, token="secret").send("owner", "context", text_item("reply"), "stable")
        msg = session.calls[0][2]["json"]["msg"]
        self.assertEqual(msg["context_token"], "context")
        self.assertEqual(msg["client_id"], "stable")
        self.assertEqual(msg["message_type"], 2)
        self.assertEqual(msg["message_state"], 2)
        with self.assertRaises(ProtocolError):
            await WeChatClient(session, token="secret").send("owner", "", text_item("reply"), "id")
        self.assertEqual(len(session.calls), 1)

    async def test_resolver_rejects_private_answers(self):
        resolver = PublicResolver()
        resolver.delegate.resolve = AsyncMock(return_value=[{"host": "127.0.0.1"}])
        with self.assertRaises(OSError):
            await resolver.resolve("ilinkai.weixin.qq.com")
        await resolver.close()

    def test_url_validation(self):
        for url in ("http://ilinkai.weixin.qq.com", "https://weixin.qq.com.evil.test",
                    "https://ilinkai.weixin.qq.com@evil.test", "https://127.0.0.1",
                    "https://ilinkai.weixin.qq.com:123", "https://ilinkai.weixin.qq.com/#secret"):
            with self.subTest(url=url), self.assertRaises(ProtocolError):
                trusted_url(url)
        self.assertEqual(trusted_url(API_BASE, api=True), API_BASE)
        with self.assertRaises(ProtocolError):
            trusted_url(API_BASE + "/path", api=True)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(Path(self.temp.name) / "wx")

    def test_permissions_and_restart(self):
        self.state.write("account.json", ACCOUNT)
        self.assertEqual(self.state.account(), ACCOUNT)
        self.assertEqual(self.state.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state.directory / "account.json").stat().st_mode & 0o777, 0o600)
        (self.state.directory / "account.json").chmod(0o644)
        with self.assertRaises(ProtocolError):
            self.state.account()

    def test_failed_atomic_write_preserves_previous(self):
        self.state.write("probe.json", {"cursor": "old"})
        with patch("app.wechat.state.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.state.write("probe.json", {"cursor": "new"})
        self.assertEqual(self.state.read("probe.json"), {"cursor": "old"})
        self.assertFalse(list(self.state.directory.glob(".write-*")))

    def test_single_poller_lock_and_release(self):
        with self.state.lock():
            with self.assertRaises(ProtocolError):
                with self.state.lock():
                    pass
        with self.state.lock():
            pass

    def test_missing_trusted_identity_is_rejected(self):
        for field in ("ilink_bot_id", "ilink_user_id", "bot_token", "baseurl"):
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                confirmed_account({**CONFIRMED, field: ""})

    def test_symlink_file_is_not_read(self):
        target = Path(self.temp.name) / "private"
        target.write_text(json.dumps(ACCOUNT))
        (self.state.directory / "account.json").symlink_to(target)
        with self.assertRaises(OSError):
            self.state.account()

    @unittest.skipUnless(importlib.util.find_spec("qrcode"), "optional QR rendering dependency")
    def test_qr_written_privately(self):
        show_qr(self.state, "https://example.test/login-test")
        path = self.state.directory / "login.png"
        with Image.open(path) as image:
            self.assertEqual(image.format, "PNG")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(Path(self.temp.name) / "wx")
        self.show = lambda *_: None

    async def perform(self, statuses, **kwargs):
        client = WeChatClient(Session(Response({"qrcode": "qr", "qrcode_img_content": "content"}),
                                      *[Response(s) for s in statuses]))
        account = await login(client, self.state, show=self.show, sleep=AsyncMock(), **kwargs)
        return account, client

    async def test_wait_scan_redirect_verify_confirm(self):
        account, client = await self.perform([
            {"status": "wait"}, {"status": "scaned_but_redirect", "redirect_host": "ilinkai2.weixin.qq.com"},
            {"status": "need_verifycode"}, {"status": "scaned"}, CONFIRMED,
        ], read_code=lambda: "1234")
        self.assertEqual(account, ACCOUNT)
        self.assertEqual(self.state.account(), ACCOUNT)
        self.assertIn("https://ilinkai2.weixin.qq.com/", client.session.calls[3][1])
        self.assertIn("verify_code=1234", client.session.calls[4][1])
        self.assertNotIn("verify_code", client.session.calls[5][1])

    async def test_expired_refresh_and_cleanup(self):
        client = WeChatClient(Session(
            Response({"qrcode": "old", "qrcode_img_content": "old"}), Response({"status": "expired"}),
            Response({"qrcode": "new", "qrcode_img_content": "new"}), Response(CONFIRMED),
        ))
        (self.state.directory / "login.png").touch()
        await login(client, self.state, show=self.show, sleep=AsyncMock())
        self.assertIn("qrcode=new", client.session.calls[-1][1])
        self.assertFalse((self.state.directory / "login.png").exists())

    async def test_blocked_unknown_and_untrusted_redirect_stop(self):
        for status in ({"status": "verify_code_blocked"}, {"status": "new-unknown"},
                       {"status": "binded_redirect"},
                       {"status": "scaned_but_redirect", "redirect_host": "evil.test"}):
            with self.subTest(status=status), self.assertRaises(ProtocolError):
                await self.perform([status])
        self.assertIsNone(self.state.account())

    async def test_existing_binding_requires_saved_account(self):
        self.state.write("account.json", ACCOUNT)
        account, client = await self.perform([{"status": "binded_redirect"}])
        self.assertEqual(account, ACCOUNT)
        self.assertEqual(client.session.calls[0][2]["json"]["local_token_list"], [ACCOUNT["token"]])

    async def test_account_switch_cannot_overwrite_saved_binding(self):
        self.state.write("account.json", ACCOUNT)
        with self.assertRaises(ProtocolError):
            await self.perform([{**CONFIRMED, "ilink_user_id": "stranger"}])
        self.assertEqual(self.state.account(), ACCOUNT)


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def test_aes_known_vector_key_encodings_and_invalid_padding(self):
        # NIST AES-128 ECB first block (PKCS7 adds a second block).
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        encrypted = crypt(plain, key)
        self.assertEqual(encrypted[:16].hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(crypt(encrypted, key, decrypt=True), plain)
        for encoded in (base64.b64encode(key), base64.b64encode(key.hex().encode())):
            self.assertEqual(decode_key(encoded), key)
        with self.assertRaises(ProtocolError):
            decode_key("not-a-key")
        with self.assertRaises(ProtocolError):
            crypt(b"bad-padding", key, decrypt=True)

    async def test_file_round_trip_integrity_and_no_cdn_credentials(self):
        data, key = b"controlled test file\n", bytes(range(16))
        session = Session(Response(raw=crypt(data, key)),
                          Response({"upload_param": "a+&b"}),
                          Response(headers={"x-encrypted-param": "download-result"}))
        media = Media(WeChatClient(session, token="private-bot-token"))
        item = {"type": 4, "file_item": {"file_name": "../../sample.txt", "len": str(len(data)),
                "md5": hashlib.md5(data).hexdigest(), "media": {"aes_key": base64.b64encode(key).decode(),
                "encrypt_query_param": "download+param"}}}
        raw, name, kind = await media.download(item)
        result = await media.upload(raw, name, kind, "owner")
        self.assertEqual(raw, data)
        self.assertEqual(name, "sample.txt")
        self.assertEqual(result["file_item"]["len"], str(len(data)))
        for index in (0, 2):
            self.assertNotIn("Authorization", session.calls[index][2]["headers"])
            self.assertFalse(session.calls[index][2]["allow_redirects"])
        upload = session.calls[1][2]["json"]
        encrypted = session.calls[2][2]["data"]
        self.assertEqual(crypt(encrypted, bytes.fromhex(upload["aeskey"]), decrypt=True), data)
        self.assertIn("a%2B%26b", str(session.calls[2][1]))

    async def test_image_hex_key_takes_precedence(self):
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(output, format="PNG")
        data, key = output.getvalue(), bytes(range(16))
        media = Media(WeChatClient(Session(Response(raw=crypt(data, key)))))
        result = await media.download({"type": 2, "image_item": {"aeskey": key.hex(), "media": {
            "aes_key": "invalid", "full_url": "https://novac2c.cdn.weixin.qq.com/test?a=1"}}})
        self.assertEqual(result[0], data)

    async def test_oversize_stream_and_host_redirect_are_rejected(self):
        for response in (Response(raw=b"x" * (MAX_MEDIA + 17), length=False), Response(status=302)):
            media = Media(WeChatClient(Session(response)))
            with self.assertRaises(ProtocolError):
                await media.transfer("GET", "https://novac2c.cdn.weixin.qq.com/file")
        media = Media(WeChatClient(Session()))
        with self.assertRaises(ProtocolError):
            await media.transfer("GET", "https://evil.test/private")
        self.assertEqual(media.client.session.calls, [])

    async def test_size_hash_invalid_image_and_missing_key_fail(self):
        key, data = bytes(range(16)), b"not a real image"
        reference = {"aes_key": base64.b64encode(key).decode(), "encrypt_query_param": "p"}
        for item in ({"type": 4, "file_item": {"len": "999", "media": reference}},
                     {"type": 4, "file_item": {"md5": "wrong", "media": reference}},
                     {"type": 2, "image_item": {"media": reference}},
                     {"type": 4, "file_item": {"media": {"encrypt_query_param": "p"}}}):
            with self.subTest(item=item), self.assertRaises(ProtocolError):
                await Media(WeChatClient(Session(Response(raw=crypt(data, key))))).download(item)

    def test_filename_cannot_escape_or_include_controls(self):
        self.assertEqual(safe_filename("..\\..\\test.txt"), "test.txt")
        self.assertNotIn("\x1b", safe_filename("hello\x1b.txt"))


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(Path(self.temp.name) / "wx")
        self.client = WeChatClient(Session(), token=ACCOUNT["token"])
        self.client.send = AsyncMock(return_value={"ret": 0})
        self.probe = Probe(self.client, self.state, ACCOUNT, sleep=AsyncMock())

    async def test_strangers_self_groups_incomplete_and_old_are_ignored(self):
        for changes in ({"from_user_id": "stranger"}, {"from_user_id": ACCOUNT["bot_id"]},
                        {"to_user_id": "another-bot"}, {"message_type": 2}, {"message_state": 1},
                        {"group_id": "group"}, {"context_token": ""}, {"message_id": None},
                        {"create_time_ms": 0}):
            self.assertIsNone(await self.probe.handle(message(**changes)))
        self.client.send.assert_not_awaited()

    def test_reference_and_arbitrary_chat_cannot_trigger_command(self):
        self.assertIsNone(action_for(message("hello", item_list=[{
            **text_item("hello"), "ref_msg": {"message_item": text_item("测试")}}])))
        self.assertIsNone(action_for(message("延迟 999")))
        self.assertEqual(action_for(message("/测试")), ("text", 0))

    def test_delay_accepts_common_keyboard_spacing_and_width(self):
        for text in ("延迟120", "延迟  120", "延迟　120", "延迟 １２０", "／延迟 120"):
            with self.subTest(text=text):
                self.assertEqual(action_for(message(text)), ("delay", 120))
        self.assertEqual(action_for(message("延迟\t300")), ("delay", 300))
        for text in ("延迟 12", "延迟 999", "延迟 120秒", "请延迟 120", "延迟 120 然后做其他事"):
            with self.subTest(text=text):
                self.assertIsNone(action_for(message(text)))

    async def test_restart_duplicate_does_not_resend(self):
        self.assertEqual(await self.probe.handle(message()), "text")
        restarted = Probe(self.client, self.state, ACCOUNT)
        self.assertIsNone(await restarted.handle(message()))
        self.assertEqual(self.client.send.await_count, 1)
        self.assertNotIn("private-context", (self.state.directory / "probe.json").read_text())

    async def test_disk_failure_prevents_send(self):
        with patch.object(self.state, "write", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                await self.probe.handle(message())
        self.client.send.assert_not_awaited()

    async def test_unknown_send_persists_and_is_not_replayed(self):
        self.client.send.side_effect = DeliveryUnknown("unknown")
        with self.assertRaises(DeliveryUnknown):
            await self.probe.handle(message())
        restarted = Probe(self.client, self.state, ACCOUNT)
        self.assertIsNone(await restarted.handle(message()))
        self.assertEqual(self.client.send.await_count, 1)
        self.assertEqual(self.state.read("probe.json")["seen"][str(message()["message_id"])], "unknown")

    async def test_delay_keeps_original_context_and_sends_only_after_wait(self):
        events = []
        async def sleep(seconds):
            events.append(("sleep", seconds))
        async def send(owner, context, item, identity):
            events.append(("send", context))
            return {}
        self.probe.sleep, self.client.send = sleep, send
        await self.probe.handle(message("延迟 120"))
        self.assertEqual(events, [("sleep", 120), ("send", "private-context")])

    async def test_cancellation_during_delay_never_resends(self):
        self.probe.sleep = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.probe.handle(message("延迟 300"))
        self.assertIsNone(await Probe(self.client, self.state, ACCOUNT).handle(message("延迟 300")))
        self.client.send.assert_not_awaited()

    async def test_media_text_and_attachment_use_same_context_separate_ids(self):
        self.probe.media.download = AsyncMock(return_value=(b"file", "sample.txt", 4))
        self.probe.media.upload = AsyncMock(return_value={"type": 4})
        await self.probe.handle(message(item_list=[{"type": 4}]))
        calls = self.client.send.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[1], calls[1].args[1])
        self.assertNotEqual(calls[0].args[3], calls[1].args[3])

    async def test_partial_send_failure_never_counts_as_complete(self):
        self.probe.media.download = AsyncMock(return_value=(b"file", "sample.txt", 4))
        self.probe.media.upload = AsyncMock(return_value={"type": 4})
        self.client.send.side_effect = [{"ret": 0}, DeliveryUnknown("timeout")]
        with self.assertRaises(DeliveryUnknown):
            await self.probe.handle(message(item_list=[{"type": 4}]))
        self.assertEqual(self.state.read("probe.json")["seen"][str(message()["message_id"])], "unknown")

    async def test_cursor_commits_only_after_batch_and_resumes_on_restart(self):
        self.client.updates = AsyncMock(side_effect=[
            {"msgs": [message()], "get_updates_buf": "cursor-after-first"},
            {"msgs": [message("结束测试", message_id=2)], "get_updates_buf": "last"},
        ])
        await self.probe.run(10, 12)
        self.assertEqual(self.client.updates.await_args_list[1].args, ("cursor-after-first",))
        self.assertEqual(Probe(self.client, self.state, ACCOUNT).saved["cursor"], "cursor-after-first")

    async def test_failed_send_does_not_advance_cursor(self):
        self.client.updates = AsyncMock(return_value={"msgs": [message()], "get_updates_buf": "new"})
        self.client.send.side_effect = DeliveryUnknown("timeout")
        with self.assertRaises(DeliveryUnknown):
            await self.probe.run(10, 12)
        self.assertEqual(self.state.read("probe.json")["cursor"], "")


if __name__ == "__main__":
    unittest.main()
