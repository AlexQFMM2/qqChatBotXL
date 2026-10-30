"""Audio-adapter checks, also runnable alone in its FFmpeg-equipped image."""
import array
import asyncio
import math
import importlib.util
import pathlib
import shutil
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from voice_adapter.server import AdapterError, _word_learning_pcm, _encode_silk_sync


class WordAudioTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and importlib.util.find_spec("pilk"), "Run SILK check in voice-adapter image")
    async def test_silk_keeps_the_extended_word_duration(self):
        import pilk
        pcm = array.array("h", (int(10000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(19200)))
        slowed = await _word_learning_pcm(pcm.tobytes())
        encoded = await asyncio.to_thread(_encode_silk_sync, slowed)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "word.silk"
            path.write_bytes(encoded)
            self.assertGreater(pilk.get_duration(str(path)), 1400)

    @unittest.skipUnless(shutil.which("ffmpeg"), "Run this DSP check in the voice-adapter image with FFmpeg")
    async def test_slower_audio_preserves_pitch_and_adds_bounded_silence(self):
        pcm = array.array("h", (int(10000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(19200)))
        result = array.array("h")
        result.frombytes(await _word_learning_pcm(pcm.tobytes()))
        self.assertTrue(1.48 < len(result) / 24000 < 1.61)
        self.assertEqual(max(abs(v) for v in result[:4800]), 0)
        self.assertEqual(max(abs(v) for v in result[-8400:]), 0)
        middle = result[7000:-11000]
        positive_crossings = sum(a <= 0 < b for a, b in zip(middle, middle[1:]))
        self.assertAlmostEqual(positive_crossings * 24000 / len(middle), 440, delta=4)

    async def test_failed_processing_does_not_return_unprocessed_audio(self):
        process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", b"error")))
        with patch("voice_adapter.server.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(AdapterError):
                await _word_learning_pcm(b"pcm")

    async def test_cancelled_processing_reaps_child_process(self):
        process = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=asyncio.CancelledError),
                                  kill=Mock(), wait=AsyncMock())
        with patch("voice_adapter.server.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                await _word_learning_pcm(b"pcm")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
