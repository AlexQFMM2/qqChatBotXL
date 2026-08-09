from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web


LOGGER = logging.getLogger(__name__)
SUPPORTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SIZE_RE = re.compile(r"^(\d{3,4})\*(\d{3,4})$")


class AdapterError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Settings:
    client_api_key: str
    dashscope_api_key: str
    dashscope_native_url: str
    allowed_models: frozenset[str]
    max_images: int
    max_image_bytes: int
    request_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"missing required environment variable: {name}")
            return value

        models = frozenset(
            item.strip()
            for item in required("ALLOWED_MODELS").split(",")
            if item.strip()
        )
        if not models:
            raise ValueError("ALLOWED_MODELS must not be empty")
        native_url = required("DASHSCOPE_NATIVE_URL")
        if not native_url.startswith("https://"):
            raise ValueError("DASHSCOPE_NATIVE_URL must use HTTPS")
        return cls(
            client_api_key=required("ADAPTER_CLIENT_KEY"),
            dashscope_api_key=required("DASHSCOPE_API_KEY"),
            dashscope_native_url=native_url,
            allowed_models=models,
            max_images=_bounded_int("MAX_IMAGES", 4, 1, 8),
            max_image_bytes=_bounded_int("MAX_IMAGE_MB", 8, 1, 20) * 1024 * 1024,
            request_timeout_seconds=_bounded_int(
                "REQUEST_TIMEOUT_SECONDS", 180, 30, 600
            ),
        )


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _decode_image(item: Any, max_bytes: int) -> dict[str, str]:
    if not isinstance(item, dict):
        raise AdapterError("images entries must be objects")
    media_type = str(item.get("media_type", "")).lower()
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise AdapterError("unsupported image media type")
    encoded = item.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise AdapterError("image data is required")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AdapterError("image data is not valid base64") from exc
    if not raw or len(raw) > max_bytes:
        raise AdapterError("image is empty or exceeds the size limit")
    return {"image": f"data:{media_type};base64,{encoded}"}


def build_upstream_payload(body: Any, settings: Settings) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AdapterError("JSON body must be an object")
    model = str(body.get("model", "")).strip()
    if model not in settings.allowed_models:
        raise AdapterError("image edit model is not allowed")
    prompt = str(body.get("prompt", "")).strip()
    if not prompt or len(prompt) > 8000:
        raise AdapterError("prompt is required and must not exceed 8000 characters")
    images = body.get("images")
    if not isinstance(images, list) or not 1 <= len(images) <= settings.max_images:
        raise AdapterError(f"images must contain 1 to {settings.max_images} items")

    content = [_decode_image(item, settings.max_image_bytes) for item in images]
    content.append({"text": prompt})
    parameters: dict[str, Any] = {
        "n": 1,
        "negative_prompt": " ",
        "prompt_extend": True,
        "watermark": False,
    }
    raw_size = body.get("size")
    if raw_size:
        match = SIZE_RE.fullmatch(str(raw_size))
        if match is None or any(not 512 <= int(value) <= 2048 for value in match.groups()):
            raise AdapterError("size must be WIDTH*HEIGHT with each side from 512 to 2048")
        parameters["size"] = str(raw_size)
    return {
        "model": model,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": parameters,
    }


def extract_image_urls(payload: Any) -> list[str]:
    try:
        contents = payload["output"]["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AdapterError("DashScope returned no image content", 502) from exc
    if not isinstance(contents, list):
        raise AdapterError("DashScope returned malformed image content", 502)
    urls: list[str] = []
    for item in contents:
        if not isinstance(item, dict):
            continue
        url = item.get("image")
        if not isinstance(url, str):
            continue
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
            and hostname.endswith(".aliyuncs.com")
        ):
            urls.append(url)
    if not urls:
        raise AdapterError("DashScope returned no trusted image URL", 502)
    return urls


def _authorized(request: web.Request, settings: Settings) -> bool:
    scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
    return bool(
        separator
        and scheme.lower() == "bearer"
        and hmac.compare_digest(token, settings.client_api_key)
    )


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def image_edits(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    if not _authorized(request, settings):
        raise AdapterError("unauthorized", 401)
    try:
        body = await request.json()
    except Exception as exc:
        raise AdapterError("request body must be valid JSON") from exc
    payload = build_upstream_payload(body, settings)
    session: aiohttp.ClientSession = request.app["session"]
    try:
        async with session.post(
            settings.dashscope_native_url,
            headers={
                "Authorization": f"Bearer {settings.dashscope_api_key}",
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

    urls = extract_image_urls(data)
    return web.json_response(
        {"model": payload["model"], "data": [{"url": url} for url in urls]}
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
        yield


def create_app(settings: Settings | None = None) -> web.Application:
    effective = settings or Settings.from_env()
    app = web.Application(
        middlewares=[error_middleware],
        client_max_size=(
            effective.max_image_bytes * effective.max_images * 4 // 3
        )
        + 2 * 1024 * 1024,
    )
    app["settings"] = effective
    app.cleanup_ctx.append(session_context)
    app.router.add_get("/health", health)
    app.router.add_post("/v1/image-edits", image_edits)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(create_app(), host="0.0.0.0", port=8081, access_log=None)


if __name__ == "__main__":
    main()
