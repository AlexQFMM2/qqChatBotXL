from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.llm import LLMClient, LLMError, ImageInput
from app.wechat.client import ProtocolError
from app.wechat.media import fit_generated_image
from app.wechat.transport import WeChatTransport
from app.channel import TransportError
from test_voice import FakeVoiceResponse, FakeVoiceSession
from voice_adapter.server import VoiceService, AdapterError, Settings


class FusionMediaTests(unittest.IsolatedAsyncioTestCase):
    def test_fusion_internal_service_url_is_case_insensitive_and_bounded(self):
        env = {'DASHSCOPE_NATIVE_URL': 'https://dashscope.aliyuncs.com/api',
               'PUBLIC_BASE_URL': 'https://voice.example.test', 'ADAPTER_CLIENT_KEY': 'test',
               'DASHSCOPE_API_KEY': 'test', 'FUSION_API_KEY': 'test'}
        with patch.dict(os.environ, env, clear=True):
            for port in (3456, 8080):
                os.environ['FUSION_BASE_URL'] = f'http://myModels:{port}/v1'
                self.assertTrue(Settings.from_env().fusion_base_url.endswith('/v1'))
            for url in ('http://example.test:8080/v1', 'http://myModels:80/v1', 'http://user@myModels:8080/v1'):
                os.environ['FUSION_BASE_URL'] = url
                with self.assertRaises(ValueError): Settings.from_env()

    def client(self):
        settings = SimpleNamespace(media_route='fusion', llm_model='Fusion/qqchatbot',
                                   llm_base_url='https://example.test', llm_api_key='client-key')
        return LLMClient(settings, FakeVoiceSession(b'\x02#!SILK_V3payload'))

    async def test_image_and_edit_use_profile_not_old_models(self):
        client = self.client()
        client._post = AsyncMock(return_value={'data': [{'url': 'https://test.aliyuncs.com/a.png'}]})
        await client.generate_image('画猫', model='obsolete-image')
        url, headers, body = client._post.await_args.args
        self.assertTrue(url.endswith('/v1/fusion/media'))
        self.assertEqual(body, {'model': 'Fusion/qqchatbot', 'operation': 'draw', 'prompt': '画猫'})
        await client.edit_image('改图', model='obsolete-edit', images=[ImageInput('image/png', b'png')])
        self.assertEqual(client._post.await_args.args[2]['operation'], 'edit')
        self.assertEqual(client._post.await_args.args[2]['model'], 'Fusion/qqchatbot')
        client._post.side_effect = LLMError('extension disabled')
        with self.assertRaises(LLMError): await client.generate_image('画猫', model='obsolete-image')
        self.assertEqual(client._post.await_count, 3)  # no direct fallback

    async def test_voice_selects_profile_and_keeps_qq_codec(self):
        client = self.client()
        await client.generate_voice('你好', model='obsolete-voice', max_bytes=1024)
        self.assertEqual(client._session.calls[0]['json']['model'], 'Fusion/qqchatbot')
        self.assertNotIn('format', client._session.calls[0]['json'])

    async def test_voice_adapter_uses_ccr_without_enrolling_or_falling_back(self):
        settings = SimpleNamespace(fusion_base_url='http://myModels:3456/v1', fusion_api_key='ccr-key', max_audio_bytes=1024*1024)
        payload = json.dumps({'output': {'audio': {'data': base64.b64encode(b'audio').decode()}}}).encode()
        calls = []
        def post(url, **kwargs):
            calls.append((url, kwargs)); return FakeVoiceResponse(payload)
        service = VoiceService(settings, SimpleNamespace(post=post))
        service.ensure_voice = AsyncMock()
        with patch('voice_adapter.server._decode_to_pcm', AsyncMock(return_value=b'\0\0'*24000)):
            data = await service.synthesize('你好', audio_format='wav', fusion_model='Fusion/qqchatbot')
        self.assertTrue(data.startswith(b'RIFF'))
        service.ensure_voice.assert_not_awaited()
        self.assertEqual(calls[0][1]['json']['operation'], 'speech')
        self.assertFalse(calls[0][1]['allow_redirects'])
        service.session.post = lambda *args, **kwargs: FakeVoiceResponse(b'private error', 502)
        with self.assertRaises(AdapterError):
            await service.synthesize('你好', fusion_model='Fusion/qqchatbot')
        service.ensure_voice.assert_not_awaited()


class GeneratedImageSizeTests(unittest.TestCase):
    def test_compresses_large_image_but_rejects_invalid_payload(self):
        import random
        raw = random.Random(8).randbytes(800*800*3)
        picture = Image.frombytes('RGB', (800,800), raw)
        output = io.BytesIO(); picture.save(output, format='PNG')
        original = output.getvalue()
        self.assertGreater(len(original), 400_000)
        result = fit_generated_image(original, 400_000)
        self.assertLessEqual(len(result), 400_000)
        with Image.open(io.BytesIO(result)) as compressed:
            compressed.verify()
        self.assertEqual(fit_generated_image(original, len(original)), original)
        with self.assertRaises(ProtocolError): fit_generated_image(b'not image', 100)


class GeneratedImageTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_can_exceed_final_quota_but_download_and_inbound_stay_bounded(self):
        import random
        buf=io.BytesIO();Image.frombytes('RGB',(600,600),random.Random(4).randbytes(600*600*3)).save(buf,format='PNG')
        source=buf.getvalue();limit=400_000
        self.assertGreater(len(source),limit)
        class Response:
            status=200
            content_length=len(source)
            @property
            def content(self):return self
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            async def iter_chunked(self,amount):
                for start in range(0,len(source),amount):yield source[start:start+amount]
        response=Response()
        transport=WeChatTransport(SimpleNamespace(session=SimpleNamespace(get=lambda *a,**k:response)),None,{'bot_id':'bot','owner':'owner'})
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'result.png'
            await transport.download_attachment('https://dashscope-test.aliyuncs.com/image.png',target,limit)
            self.assertLessEqual(target.stat().st_size,limit)
            with Image.open(target) as picture:picture.verify()
            target.unlink()
            response.content_length=21*1024*1024
            with self.assertRaises(TransportError):
                await transport.download_attachment('https://dashscope-test.aliyuncs.com/image.png',target,limit)
            self.assertFalse(target.exists())
            transport.active={'item_list':[{}]}
            transport.media.download=AsyncMock(return_value=(source,'large.png',2))
            with self.assertRaises(TransportError):
                await transport.download_attachment('wechat-attachment:0',target,limit)
            self.assertFalse(target.exists())


if __name__ == '__main__': unittest.main()
