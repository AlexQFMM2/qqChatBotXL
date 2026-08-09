from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.bot import PersonaBot
from app.llm import LLMClient, LLMError
from voice_adapter.server import (
    AdapterError,
    Settings,
    build_enrollment_payload,
    build_speech_payload,
    extract_audio_result,
    extract_voice_id,
)


class VoiceAdapterPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(
            client_api_key="client-secret",
            dashscope_api_key="upstream-secret",
            dashscope_native_url=(
                "https://workspace.cn-beijing.maas.aliyuncs.com/"
                "api/v1/services/aigc/multimodal-generation/generation"
            ),
            public_base_url="https://voice.example.test",
            voice_model="qwen3-tts-vc-2026-01-22",
            enrollment_model="qwen-voice-enrollment",
            voice_prefix="shirley",
            reference_path=Path(self.directory.name) / "reference.wav",
            state_path=Path(self.directory.name) / "voice_id.txt",
            max_text_chars=240,
            max_audio_bytes=1024 * 1024,
            request_timeout_seconds=180,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_builds_enrollment_and_speech_payloads(self) -> None:
        enrollment = build_enrollment_payload(self.settings)
        self.assertEqual(enrollment["input"]["target_model"], self.settings.voice_model)
        self.assertEqual(enrollment["input"]["preferred_name"], "shirley")
        reference_url = enrollment["input"]["audio"]["data"]
        self.assertIn("/v1/voice-reference/", reference_url)
        self.assertNotIn("client-secret", reference_url)

        speech = build_speech_payload("早上好，老师。", "shirley-demo-001", self.settings)
        self.assertEqual(speech["input"]["voice"], "shirley-demo-001")
        self.assertEqual(speech["input"]["text"], "早上好，老师。")

    def test_extracts_voice_id_and_trusted_audio(self) -> None:
        self.assertEqual(
            extract_voice_id({"output": {"voice": "shirley-demo-001"}}),
            "shirley-demo-001",
        )
        url, embedded = extract_audio_result(
            {
                "output": {
                    "audio": {
                        "url": "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav"
                    }
                }
            },
            1024,
        )
        self.assertIsNone(embedded)
        self.assertTrue(url and url.endswith("a.wav"))

        raw = b"RIFFaudio"
        url, embedded = extract_audio_result(
            {"output": {"audio": {"data": base64.b64encode(raw).decode()}}},
            1024,
        )
        self.assertIsNone(url)
        self.assertEqual(embedded, raw)

        url, embedded = extract_audio_result(
            {
                "output": {
                    "audio": "http://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav"
                }
            },
            1024,
        )
        self.assertIsNone(embedded)
        self.assertTrue(url and url.endswith("a.wav"))
        self.assertTrue(url and url.startswith("https://"))

    def test_rejects_untrusted_audio_url_and_long_text(self) -> None:
        with self.assertRaises(AdapterError):
            extract_audio_result(
                {"output": {"audio": {"url": "https://evil.example/audio.wav"}}},
                1024,
            )
        with self.assertRaises(AdapterError):
            build_speech_payload("太" * 241, "shirley-demo-001", self.settings)


class FakeContent:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    async def read(self, amount: int) -> bytes:
        chunk = self.data[self.offset : self.offset + amount]
        self.offset += len(chunk)
        return chunk


class FakeVoiceResponse:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self.status = status
        self.content_length = len(data)
        self.content = FakeContent(data)
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self) -> str:
        return self._data.decode("utf-8", "replace")


class FakeVoiceSession:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls: list[dict] = []

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeVoiceResponse(self.data)


class VoiceClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_tencent_silk(self) -> None:
        session = FakeVoiceSession(b"\x02#!SILK_V3payload")
        settings = SimpleNamespace(
            llm_base_url="https://gateway.example.test",
            llm_api_key="key",
        )
        client = LLMClient(settings, session)  # type: ignore[arg-type]
        result = await client.generate_voice(
            "早上好，老师。", model="qwen3-tts-vc-2026-01-22", max_bytes=1024
        )
        self.assertTrue(result.startswith(b"\x02#!SILK_V3"))
        self.assertTrue(session.calls[0]["url"].endswith("/v1/voice-speech"))

    async def test_reads_all_response_chunks(self) -> None:
        data = b"\x02#!SILK_V3" + b"a" * (70 * 1024)
        session = FakeVoiceSession(data)
        settings = SimpleNamespace(
            llm_base_url="https://gateway.example.test",
            llm_api_key="key",
        )
        client = LLMClient(settings, session)  # type: ignore[arg-type]
        result = await client.generate_voice("测试", model="voice", max_bytes=80 * 1024)
        self.assertEqual(result, data)

    async def test_rejects_non_silk_response(self) -> None:
        session = FakeVoiceSession(b"not audio")
        settings = SimpleNamespace(
            llm_base_url="https://gateway.example.test",
            llm_api_key="key",
        )
        client = LLMClient(settings, session)  # type: ignore[arg-type]
        with self.assertRaises(LLMError):
            await client.generate_voice("test", model="voice", max_bytes=1024)


class VoiceIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = PersonaBot.__new__(PersonaBot)
        self.bot._settings = SimpleNamespace(  # type: ignore[attr-defined]
            bot_aliases=("夏莉", "Shirley")
        )

    def test_detects_explicit_voice_requests(self) -> None:
        self.assertTrue(self.bot._explicit_voice_request("夏莉，请用语音回复我"))
        self.assertTrue(self.bot._explicit_voice_request("/voice 早上好，老师"))
        self.assertTrue(self.bot._explicit_voice_request("说句话给我听听"))

    def test_ignores_unrelated_voice_discussion(self) -> None:
        self.assertFalse(self.bot._explicit_voice_request("这个配音演员是谁？"))
        self.assertFalse(self.bot._explicit_voice_request("/voice"))


if __name__ == "__main__":
    unittest.main()
