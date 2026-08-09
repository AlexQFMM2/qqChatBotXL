from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web


LOGGER = logging.getLogger(__name__)
VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{6,256}$")


class AdapterError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Settings:
    client_api_key: str
    dashscope_api_key: str
    dashscope_native_url: str
    public_base_url: str
    voice_model: str
    enrollment_model: str
    voice_prefix: str
    reference_path: Path
    state_path: Path
    max_text_chars: int
    max_audio_bytes: int
    request_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"missing required environment variable: {name}")
            return value

        native_url = required("DASHSCOPE_NATIVE_URL")
        public_base_url = required("PUBLIC_BASE_URL").rstrip("/")
        for name, url in (
            ("DASHSCOPE_NATIVE_URL", native_url),
            ("PUBLIC_BASE_URL", public_base_url),
        ):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
            ):
                raise ValueError(f"{name} must be a public HTTPS URL")
        prefix = os.getenv("VOICE_PREFIX", "shirley").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9]{2,15}", prefix):
            raise ValueError("VOICE_PREFIX must be 3-16 lowercase letters or digits")
        return cls(
            client_api_key=required("ADAPTER_CLIENT_KEY"),
            dashscope_api_key=required("DASHSCOPE_API_KEY"),
            dashscope_native_url=native_url,
            public_base_url=public_base_url,
            voice_model=os.getenv(
                "VOICE_MODEL", "qwen3-tts-vc-2026-01-22"
            ).strip()
            or "qwen3-tts-vc-2026-01-22",
            enrollment_model=os.getenv(
                "VOICE_ENROLLMENT_MODEL", "qwen-voice-enrollment"
            ).strip()
            or "qwen-voice-enrollment",
            voice_prefix=prefix,
            reference_path=Path(
                os.getenv("VOICE_REFERENCE_PATH", "/adapter/reference.wav")
            ),
            state_path=Path(os.getenv("VOICE_STATE_PATH", "/data/voice_id.txt")),
            max_text_chars=_bounded_int("MAX_TEXT_CHARS", 240, 20, 1000),
            max_audio_bytes=_bounded_int("MAX_AUDIO_MB", 12, 1, 30)
            * 1024
            * 1024,
            request_timeout_seconds=_bounded_int(
                "REQUEST_TIMEOUT_SECONDS", 180, 30, 600
            ),
        )

    @property
    def enrollment_url(self) -> str:
        parsed = urlsplit(self.dashscope_native_url)
        return (
            f"{parsed.scheme}://{parsed.netloc}"
            "/api/v1/services/audio/tts/customization"
        )

    @property
    def reference_token(self) -> str:
        return hmac.new(
            self.client_api_key.encode("utf-8"),
            b"qqchat-shirley-voice-reference-v1",
            hashlib.sha256,
        ).hexdigest()

    @property
    def reference_url(self) -> str:
        return f"{self.public_base_url}/v1/voice-reference/{self.reference_token}"


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def build_enrollment_payload(settings: Settings) -> dict[str, Any]:
    return {
        "model": settings.enrollment_model,
        "input": {
            "action": "create",
            "target_model": settings.voice_model,
            "preferred_name": settings.voice_prefix,
            "audio": {"data": settings.reference_url},
        },
    }


def build_speech_payload(text: str, voice_id: str, settings: Settings) -> dict[str, Any]:
    value = text.strip()
    if not value or len(value) > settings.max_text_chars:
        raise AdapterError(
            f"text is required and must not exceed {settings.max_text_chars} characters"
        )
    if not VOICE_ID_RE.fullmatch(voice_id):
        raise AdapterError("cached voice id is invalid", 500)
    return {
        "model": settings.voice_model,
        "input": {"text": value, "voice": voice_id},
    }


def extract_voice_id(payload: Any) -> str:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        output = payload.get("output")
        if isinstance(output, dict):
            candidates.extend(
                output.get(key) for key in ("voice", "voice_id", "voiceId")
            )
        candidates.extend(payload.get(key) for key in ("voice", "voice_id", "voiceId"))
    for candidate in candidates:
        value = str(candidate or "").strip()
        if VOICE_ID_RE.fullmatch(value):
            return value
    raise AdapterError("DashScope returned no usable voice id", 502)


def _normalized_trusted_audio_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower()
    trusted_host = bool(
        hostname.endswith(".aliyuncs.com")
        or hostname.endswith(".alibabacloud.com")
    )
    if not (
        parsed.scheme in {"http", "https"}
        and not parsed.username
        and not parsed.password
        and port in {None, 443}
        and trusted_host
    ):
        return None
    # DashScope currently returns an HTTP OSS result URL. Fetch it only after
    # upgrading the trusted provider URL to TLS; never follow arbitrary HTTP.
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return parsed.geturl()


def _trusted_audio_url(value: str) -> bool:
    return _normalized_trusted_audio_url(value) is not None


def _json_shape(value: Any, depth: int = 0) -> Any:
    """Describe response fields without logging URLs, audio, IDs, or secrets."""
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            str(key)[:80]: _json_shape(item, depth + 1)
            for key, item in list(value.items())[:30]
        }
    if isinstance(value, list):
        return [
            _json_shape(item, depth + 1) for item in value[:3]
        ] + ([f"... {len(value) - 3} more"] if len(value) > 3 else [])
    return type(value).__name__


def extract_audio_result(payload: Any, max_bytes: int) -> tuple[str | None, bytes | None]:
    roots: list[Any] = [payload]
    if isinstance(payload, dict):
        roots.insert(0, payload.get("output"))
    for root in roots:
        if not isinstance(root, dict):
            continue
        audio = root.get("audio")
        direct_urls = [
            audio,
            root.get("audio_url"),
            root.get("audioUrl"),
            root.get("url"),
        ]
        for value in direct_urls:
            if isinstance(value, str):
                normalized = _normalized_trusted_audio_url(value)
                if normalized is not None:
                    return normalized, None
        candidates = [audio, root]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            url = candidate.get("url")
            if isinstance(url, str):
                normalized = _normalized_trusted_audio_url(url)
                if normalized is not None:
                    return normalized, None
            encoded = candidate.get("data")
            if isinstance(encoded, str) and encoded:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise AdapterError("DashScope returned invalid audio base64", 502) from exc
                if not raw or len(raw) > max_bytes:
                    raise AdapterError("DashScope audio is empty or too large", 502)
                return None, raw
    raise AdapterError(
        f"DashScope returned no trusted audio result (structure: {_json_shape(payload)})",
        502,
    )


def _authorized(request: web.Request, settings: Settings) -> bool:
    scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
    return bool(
        separator
        and scheme.lower() == "bearer"
        and hmac.compare_digest(token, settings.client_api_key)
    )


async def _read_limited(content: Any, max_bytes: int) -> bytes:
    data = bytearray()
    while len(data) <= max_bytes:
        chunk = await content.read(min(64 * 1024, max_bytes + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


async def _decode_to_pcm(audio: bytes) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "24000",
        "-ac",
        "1",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(audio)
    if process.returncode != 0 or not stdout:
        detail = stderr.decode("utf-8", "replace")[-300:]
        raise AdapterError(f"ffmpeg could not decode generated audio: {detail}", 502)
    return stdout


def _encode_silk_sync(pcm: bytes) -> bytes:
    try:
        import pilk
    except ImportError as exc:  # pragma: no cover - container dependency
        raise AdapterError("SILK encoder is unavailable", 500) from exc
    with tempfile.TemporaryDirectory(prefix="qqchat-silk-") as directory:
        root = Path(directory)
        pcm_path = root / "input.pcm"
        silk_path = root / "output.silk"
        pcm_path.write_bytes(pcm)
        try:
            pilk.encode(str(pcm_path), str(silk_path), pcm_rate=24000, tencent=True)
        except Exception as exc:
            raise AdapterError("SILK encoding failed", 502) from exc
        data = silk_path.read_bytes() if silk_path.is_file() else b""
        silk_duration_ms = pilk.get_duration(str(silk_path)) if data else 0
    pcm_duration_ms = len(pcm) * 1000 // (24000 * 2)
    if not (data.startswith(b"#!SILK_V3") or data.startswith(b"\x02#!SILK_V3")):
        raise AdapterError("SILK encoder returned invalid data", 502)
    if silk_duration_ms < max(120, pcm_duration_ms - 80):
        raise AdapterError("SILK encoder returned truncated audio", 502)
    return data


async def audio_to_silk(audio: bytes) -> bytes:
    pcm = await _decode_to_pcm(audio)
    return await asyncio.to_thread(_encode_silk_sync, pcm)


class VoiceService:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self.session = session
        self._enrollment_lock = asyncio.Lock()

    def cached_voice_id(self) -> str | None:
        path = self.settings.state_path
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AdapterError("could not read voice state", 500) from exc
        if not VOICE_ID_RE.fullmatch(value):
            raise AdapterError("stored voice id is invalid", 500)
        return value

    async def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self.session.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.settings.dashscope_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    detail = ""
                    if isinstance(data, dict):
                        detail = str(data.get("message") or data.get("code") or "")[:300]
                    raise AdapterError(
                        f"DashScope request failed (HTTP {response.status}): {detail}",
                        502,
                    )
        except AdapterError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise AdapterError("DashScope request failed or timed out", 502) from exc
        if not isinstance(data, dict):
            raise AdapterError("DashScope returned malformed JSON", 502)
        return data

    async def ensure_voice(self) -> str:
        cached = self.cached_voice_id()
        if cached:
            return cached
        async with self._enrollment_lock:
            cached = self.cached_voice_id()
            if cached:
                return cached
            if not self.settings.reference_path.is_file():
                raise AdapterError("voice reference file is missing", 500)
            data = await self._post_json(
                self.settings.enrollment_url,
                build_enrollment_payload(self.settings),
            )
            voice_id = extract_voice_id(data)
            path = self.settings.state_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.part")
            temporary.write_text(voice_id + "\n", encoding="utf-8")
            temporary.replace(path)
            LOGGER.info("voice enrollment completed")
            return voice_id

    async def synthesize(self, text: str) -> bytes:
        voice_id = await self.ensure_voice()
        payload = build_speech_payload(text, voice_id, self.settings)
        data = await self._post_json(self.settings.dashscope_native_url, payload)
        audio_url, embedded = extract_audio_result(data, self.settings.max_audio_bytes)
        spoken_units = len(re.findall(r"[\w\u3400-\u9fff]", text))
        minimum_duration_ms = min(1800, max(180, spoken_units * 70))
        if embedded is not None:
            audio = embedded
            pcm = await _decode_to_pcm(audio)
        else:
            assert audio_url is not None
            try:
                async with self.session.get(audio_url) as response:
                    if response.status >= 400:
                        raise AdapterError(
                            f"audio download failed (HTTP {response.status})", 502
                        )
                    declared = response.content_length
                    if (
                        declared is not None
                        and declared > self.settings.max_audio_bytes
                    ):
                        raise AdapterError("generated audio exceeds size limit", 502)
                    audio = await _read_limited(
                        response.content, self.settings.max_audio_bytes
                    )
            except AdapterError:
                raise
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise AdapterError("audio download failed or timed out", 502) from exc
            if not audio or len(audio) > self.settings.max_audio_bytes:
                raise AdapterError("generated audio is empty or too large", 502)
            pcm = await _decode_to_pcm(audio)
        if len(pcm) * 1000 // (24000 * 2) < minimum_duration_ms:
            raise AdapterError("generated audio is implausibly short", 502)
        silk = await asyncio.to_thread(_encode_silk_sync, pcm)
        if len(silk) > self.settings.max_audio_bytes:
            raise AdapterError("encoded SILK exceeds size limit", 502)
        LOGGER.info(
            "voice audio prepared: source=%d bytes, pcm=%d bytes, silk=%d bytes, duration=%d ms",
            len(audio),
            len(pcm),
            len(silk),
            len(pcm) * 1000 // (24000 * 2),
        )
        return silk


async def health(request: web.Request) -> web.Response:
    service: VoiceService = request.app["voice_service"]
    return web.json_response(
        {"status": "ok", "voice_enrolled": service.cached_voice_id() is not None}
    )


async def voice_reference(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    service: VoiceService = request.app["voice_service"]
    token = request.match_info.get("token", "")
    if not hmac.compare_digest(token, settings.reference_token):
        raise web.HTTPNotFound()
    if service.cached_voice_id() is not None:
        raise web.HTTPNotFound()
    path = settings.reference_path
    if path.is_symlink() or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "audio/wav",
            "Cache-Control": "private, no-store",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )


async def voice_speech(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    if not _authorized(request, settings):
        raise AdapterError("unauthorized", 401)
    try:
        body = await request.json()
    except Exception as exc:
        raise AdapterError("request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise AdapterError("JSON body must be an object")
    if str(body.get("model", "")).strip() != settings.voice_model:
        raise AdapterError("voice model is not allowed")
    text = str(body.get("text", "")).strip()
    if not text or len(text) > settings.max_text_chars:
        raise AdapterError(
            f"text is required and must not exceed {settings.max_text_chars} characters"
        )
    service: VoiceService = request.app["voice_service"]
    silk = await service.synthesize(text)
    return web.Response(
        body=silk,
        content_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except AdapterError as exc:
        LOGGER.warning("request rejected: %s", exc)
        return web.json_response({"error": {"message": str(exc)}}, status=exc.status)
    except web.HTTPException:
        raise
    except Exception:
        LOGGER.exception("unexpected adapter error")
        return web.json_response(
            {"error": {"message": "internal adapter error"}}, status=500
        )


async def session_context(app: web.Application):
    settings: Settings = app["settings"]
    timeout = aiohttp.ClientTimeout(total=settings.request_timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        app["session"] = session
        app["voice_service"] = VoiceService(settings, session)
        yield


def create_app(settings: Settings | None = None) -> web.Application:
    effective = settings or Settings.from_env()
    app = web.Application(middlewares=[error_middleware], client_max_size=64 * 1024)
    app["settings"] = effective
    app.cleanup_ctx.append(session_context)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/voice-reference/{token}", voice_reference)
    app.router.add_post("/v1/voice-speech", voice_speech)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(create_app(), host="0.0.0.0", port=8082, access_log=None)


if __name__ == "__main__":
    main()
