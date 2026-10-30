from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import aiohttp

from .config import Settings


LOGGER = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMOutputLimitError(LLMError):
    """A length-limited response must not be delivered or executed as complete."""


@dataclass(frozen=True, slots=True)
class FinalToolReply:
    """A validated reply owned by the application; no further model/tools are needed."""

    text: str


class VisionInputError(ValueError):
    pass


_MARKDOWN_IMAGE_URL_RE = re.compile(r"https://[^\s\]\[()<>\"']+")


@dataclass(frozen=True, slots=True)
class ImageInput:
    media_type: str
    data: bytes

    @classmethod
    def from_path(cls, path: Path, max_bytes: int) -> "ImageInput":
        if path.is_symlink() or not path.is_file():
            raise VisionInputError("图片文件不存在或不是普通文件")
        size = path.stat().st_size
        if size <= 0:
            raise VisionInputError("图片文件为空")
        if size > max_bytes:
            raise VisionInputError("图片超过识图大小限制")
        try:
            with path.open("rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError as exc:
            raise VisionInputError("无法读取图片文件") from exc
        if len(data) > max_bytes:
            raise VisionInputError("图片超过识图大小限制")
        media_type = _detect_image_media_type(data)
        if media_type is None:
            raise VisionInputError("只支持 JPEG、PNG、GIF 或 WebP 图片")
        return cls(media_type=media_type, data=data)


def _detect_image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _anthropic_user_content(text: str, images: Sequence[ImageInput]) -> str | list[dict]:
    if not images:
        return text
    content: list[dict] = []
    for image in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.media_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": text})
    return content


def _openai_user_content(text: str, images: Sequence[ImageInput]) -> str | list[dict]:
    if not images:
        return text
    content: list[dict] = [{"type": "text", "text": text}]
    for image in images:
        encoded = base64.b64encode(image.data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image.media_type};base64,{encoded}"},
            }
        )
    return content


class LLMClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session

    async def complete(
        self,
        system: str,
        user_prompt: str,
        *,
        model: str | None = None,
        images: Sequence[ImageInput] = (),
        api_format: str | None = None,
    ) -> str:
        effective_format = api_format or self._settings.llm_api_format
        if effective_format == "openai":
            return await self._openai(system, user_prompt, model=model, images=images)
        return await self._anthropic(system, user_prompt, model=model, images=images)

    async def complete_with_tools(
        self,
        system: str,
        user_prompt: str,
        tools: list[dict],
        execute_tool: Callable[[str, dict], Awaitable[str | FinalToolReply]],
        *,
        model: str | None = None,
        images: Sequence[ImageInput] = (),
        api_format: str | None = None,
        max_rounds: int = 10,
    ) -> str:
        """Run the controller's tool loop in either supported API format."""
        effective_format = api_format or self._settings.llm_api_format
        if not tools:
            return await self.complete(
                system,
                user_prompt,
                model=model,
                images=images,
                api_format=effective_format,
            )
        if effective_format != "anthropic":
            return await self._openai_with_tools(system, user_prompt, tools, execute_tool,
                                                model=model, images=images, max_rounds=max_rounds)

        url = self._endpoint("messages")
        headers = {
            "x-api-key": self._settings.llm_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        messages: list[dict] = [
            {"role": "user", "content": _anthropic_user_content(user_prompt, images)}
        ]
        for _ in range(max_rounds):
            payload = {
                "model": model or self._settings.llm_model,
                "max_tokens": self._settings.llm_max_tokens,
                "temperature": self._settings.llm_temperature,
                "thinking": {"type": "disabled"},
                "system": system,
                "messages": messages,
                "tools": tools,
            }
            data = await self._chat_post(url, headers, payload)
            content = data.get("content") or []
            if not isinstance(content, list):
                raise LLMError("工具调用响应格式不正确")
            tool_uses = [part for part in content if part.get("type") == "tool_use"]
            text = "".join(
                str(part.get("text", "")) for part in content if part.get("type") == "text"
            ).strip()
            if not tool_uses:
                if text:
                    return text
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "你刚才没有输出可发送的文字。现在不要再调用工具；"
                                    "请根据已有对话和工具结果，直接给出一段完整、自然的最终回复。"
                                ),
                            }
                        ],
                    }
                )
                recovery_payload = {
                    "model": model or self._settings.llm_model,
                    "max_tokens": self._settings.llm_max_tokens,
                    "temperature": min(self._settings.llm_temperature, 0.4),
                    "thinking": {"type": "disabled"},
                    "system": system,
                    "messages": messages,
                }
                recovery_data = await self._chat_post(url, headers, recovery_payload)
                recovery_content = recovery_data.get("content") or []
                recovery_text = "".join(
                    str(part.get("text", ""))
                    for part in recovery_content
                    if part.get("type") == "text"
                ).strip()
                if not recovery_text:
                    self._log_empty_response("tool_recovery", recovery_data)
                    raise LLMError("模型执行工具后重试仍没有返回文本")
                return recovery_text

            messages.append({"role": "assistant", "content": content})
            results = []
            for call in tool_uses:
                name = str(call.get("name", ""))
                arguments = call.get("input") if isinstance(call.get("input"), dict) else {}
                try:
                    result = await execute_tool(name, arguments)
                    if isinstance(result, FinalToolReply):
                        return result.text
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": str(call.get("id", "")),
                            "content": result,
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": str(call.get("id", "")),
                            "content": str(exc)[:500],
                            "is_error": True,
                        }
                    )
            messages.append({"role": "user", "content": results})

        final_instruction = {
            "type": "text",
            "text": (
                "工具调用轮次已达到上限。现在不要再调用任何工具；请根据已有工具结果，"
                "明确汇总已经完成的内容、尚未完成的内容和原因。"
            ),
        }
        last_content = messages[-1].get("content")
        if isinstance(last_content, list):
            last_content.append(final_instruction)
        else:
            messages.append({"role": "user", "content": [final_instruction]})
        final_payload = {
            "model": model or self._settings.llm_model,
            "max_tokens": self._settings.llm_max_tokens,
            "temperature": self._settings.llm_temperature,
            "thinking": {"type": "disabled"},
            "system": system,
            "messages": messages,
        }
        final_data = await self._chat_post(url, headers, final_payload)
        final_content = final_data.get("content") or []
        final_text = "".join(
            str(part.get("text", ""))
            for part in final_content
            if part.get("type") == "text"
        ).strip()
        if not final_text:
            raise LLMError("工具调用达到上限后，模型没有返回任务总结")
        return final_text

    async def _openai_with_tools(self, system, user_prompt, tools, execute_tool, *,
                                 model, images, max_rounds):
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": _openai_user_content(user_prompt, images)},
        ]
        definitions = [{"type": "function", "function": {
            "name": tool["name"], "description": tool["description"],
            "parameters": tool["input_schema"],
        }} for tool in tools]
        headers = {"Authorization": f"Bearer {self._settings.llm_api_key}",
                   "content-type": "application/json"}

        async def request(with_tools):
            payload = {"model": model or self._settings.llm_model,
                       "max_tokens": self._settings.llm_max_tokens,
                       "temperature": self._settings.llm_temperature, "messages": messages}
            if with_tools:
                payload["tools"] = definitions
            data = await self._chat_post(self._endpoint("chat/completions"), headers, payload)
            try:
                message = data["choices"][0]["message"]
                if not isinstance(message, dict):
                    raise TypeError()
                return message
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError("工具调用响应格式不正确") from exc

        for _ in range(max_rounds):
            message = await request(True)
            calls = message.get("tool_calls") or []
            if not calls:
                text = message.get("content")
                if isinstance(text, str) and text.strip():
                    return text.strip()
                break
            # Preserve provider reasoning fields needed by some compatible models.
            messages.append({**message, "role": "assistant"})
            for call in calls:
                try:
                    function = call["function"]
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须为对象")
                    result = await execute_tool(function["name"], arguments)
                    if isinstance(result, FinalToolReply):
                        return result.text
                except Exception as exc:
                    result = json.dumps({"error": str(exc)[:500]}, ensure_ascii=False)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        messages.append({"role": "user", "content":
            "不要再调用工具。根据已有结果回复已完成和未完成的内容；没有成功结果时不得声称成功。"})
        final = await request(False)
        text = final.get("content")
        if not isinstance(text, str) or not text.strip():
            raise LLMError("工具执行后模型没有返回最终文本")
        return text.strip()

    @staticmethod
    def _log_empty_response(stage: str, data: dict) -> None:
        content = data.get("content")
        content_types = (
            [str(part.get("type", "unknown")) for part in content if isinstance(part, dict)]
            if isinstance(content, list)
            else []
        )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        LOGGER.warning(
            "模型空响应：stage=%s stop_reason=%s content_types=%s output_tokens=%s",
            stage,
            data.get("stop_reason"),
            content_types,
            usage.get("output_tokens"),
        )

    async def generate_image(self, prompt: str, *, model: str) -> str:
        """Generate an image through the gateway's OpenAI-compatible chat API."""
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
            "max_tokens": 500,
        }
        headers = {
            "Authorization": f"Bearer {self._settings.llm_api_key}",
            "content-type": "application/json",
        }
        endpoint = "chat/completions"
        if getattr(self._settings, "media_route", "direct") == "fusion":
            endpoint = "fusion/media"
            payload = {"model": self._settings.llm_model, "operation": "draw", "prompt": prompt}
        data = await self._post(self._endpoint(endpoint), headers, payload, timeout_seconds=170)
        image_url = _generated_image_url(data)
        if image_url is None:
            fields = ", ".join(sorted(str(key) for key in data))[:160]
            raise LLMError(
                f"图片生成模型没有返回可下载的图片（响应字段：{fields or '空'}）"
            )
        if not image_url.startswith("https://"):
            raise LLMError("图片生成模型返回了不安全的下载地址")
        return image_url

    async def generate_voice(
        self,
        text: str,
        *,
        model: str,
        max_bytes: int,
        audio_format: str = "silk",
        mode: str = "speech",
    ) -> bytes:
        """Synthesize a short reply through the authenticated voice adapter."""
        value = text.strip()
        if not value:
            raise LLMError("语音文本不能为空")
        if audio_format not in {"silk", "wav"}:
            raise LLMError("不支持的语音输出格式")
        if mode not in {"speech", "word"}:
            raise LLMError("不支持的朗读方式")
        payload = {"model": model, "text": value}
        if mode != "speech":
            payload["mode"] = mode
        if getattr(self._settings, "media_route", "direct") == "fusion":
            payload["model"] = self._settings.llm_model
        if audio_format != "silk":
            payload["format"] = audio_format
        headers = {
            "Authorization": f"Bearer {self._settings.llm_api_key}",
            "content-type": "application/json",
        }
        try:
            async with asyncio.timeout(180):
                async with self._session.post(
                    self._endpoint("voice-speech"), headers=headers, json=payload
                ) as response:
                    if response.status >= 400:
                        detail = (await response.text())[:500]
                        raise LLMError(
                            f"语音适配接口 HTTP {response.status}: {detail}"
                        )
                    declared = response.content_length
                    if declared is not None and declared > max_bytes:
                        raise LLMError("生成的语音超过大小限制")
                    chunks = bytearray()
                    while len(chunks) <= max_bytes:
                        chunk = await response.content.read(
                            min(64 * 1024, max_bytes + 1 - len(chunks))
                        )
                        if not chunk:
                            break
                        chunks.extend(chunk)
                    data = bytes(chunks)
        except LLMError:
            raise
        except TimeoutError as exc:
            raise LLMError("语音生成超时") from exc
        except aiohttp.ClientError as exc:
            raise LLMError(f"语音适配接口连接失败：{exc}") from exc
        if not data or len(data) > max_bytes:
            raise LLMError("生成的语音为空或超过大小限制")
        if audio_format == "silk":
            if not (data.startswith(b"#!SILK_V3") or data.startswith(b"\x02#!SILK_V3")):
                raise LLMError("语音适配接口没有返回有效的 QQ SILK 音频")
        else:
            try:
                with wave.open(io.BytesIO(data), "rb") as audio:
                    frames = audio.getnframes()
                    if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000) \
                            or frames <= 0 or len(audio.readframes(frames)) != frames * 2:
                        raise ValueError
            except (wave.Error, EOFError, ValueError):
                raise LLMError("语音适配接口没有返回完整的 WAV 音频") from None
        return data

    async def edit_image(
        self,
        prompt: str,
        *,
        model: str,
        images: Sequence[ImageInput],
    ) -> str:
        """Edit/generate an image through the authenticated native adapter."""
        if not images:
            raise LLMError("参考图编辑至少需要一张图片")
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [
                {
                    "media_type": image.media_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                }
                for image in images
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._settings.llm_api_key}",
            "content-type": "application/json",
        }
        endpoint = "image-edits"
        if getattr(self._settings, "media_route", "direct") == "fusion":
            endpoint = "fusion/media"
            payload.update(model=self._settings.llm_model, operation="edit")
        data = await self._post(self._endpoint(endpoint), headers, payload, timeout_seconds=170)
        image_url = _generated_image_url(data)
        if image_url is None:
            raise LLMError("图像编辑模型没有返回可下载的图片")
        if not image_url.startswith("https://"):
            raise LLMError("图像编辑模型返回了不安全的下载地址")
        return image_url

    async def _anthropic(
        self,
        system: str,
        user_prompt: str,
        *,
        model: str | None,
        images: Sequence[ImageInput],
    ) -> str:
        url = self._endpoint("messages")
        headers = {
            "x-api-key": self._settings.llm_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model or self._settings.llm_model,
            "max_tokens": self._settings.llm_max_tokens,
            "temperature": self._settings.llm_temperature,
            "thinking": {"type": "disabled"},
            "system": system,
            "messages": [
                {"role": "user", "content": _anthropic_user_content(user_prompt, images)}
            ],
        }
        data = await self._chat_post(url, headers, payload)
        parts = data.get("content") or []
        text = "".join(str(part.get("text", "")) for part in parts if part.get("type") == "text")
        if not text.strip():
            self._log_empty_response("anthropic_initial", data)
            recovery_payload = {
                **payload,
                "max_tokens": self._settings.llm_max_tokens,
                "temperature": min(self._settings.llm_temperature, 0.4),
                "system": (
                    system
                    + "\n\n恢复指令：上一轮没有产生可见文本。立即输出可直接发送给用户的最终回答；"
                    "不要继续隐藏分析，不要调用工具，不要只输出思考过程。"
                ),
            }
            recovery_data = await self._chat_post(url, headers, recovery_payload)
            recovery_parts = recovery_data.get("content") or []
            text = "".join(
                str(part.get("text", ""))
                for part in recovery_parts
                if part.get("type") == "text"
            )
            if not text.strip():
                self._log_empty_response("anthropic_recovery", recovery_data)
                raise LLMError("模型重试后仍没有返回文本内容")
        return text

    async def _openai(
        self,
        system: str,
        user_prompt: str,
        *,
        model: str | None,
        images: Sequence[ImageInput],
    ) -> str:
        url = self._endpoint("chat/completions")
        headers = {
            "Authorization": f"Bearer {self._settings.llm_api_key}",
            "content-type": "application/json",
        }
        payload = {
            "model": model or self._settings.llm_model,
            "max_tokens": self._settings.llm_max_tokens,
            "temperature": self._settings.llm_temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": _openai_user_content(user_prompt, images)},
            ],
        }
        data = await self._chat_post(url, headers, payload)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("模型返回格式不正确") from exc
        if not str(text).strip():
            raise LLMError("模型返回中没有文本内容")
        return str(text)

    async def _chat_post(self, url: str, headers: dict, payload: dict) -> dict:
        """Retry a clipped model response once, before any text/tool is consumed."""
        instruction = (
            "\n\n上一份模型输出因达到生成额度而中止，未作为完整回复或工具调用执行。"
            f"本次仍不得超过 {payload['max_tokens']} tokens，不增加额度。"
            "立即输出精简且完整的最终回答，普通回复尽量在 300 个字符内概括全部要点并收尾；"
            "不展开内部推理，不复制半截内容，不添加旁枝。"
            "单词卡片仍须使用规定工具并保留全部字段；确需调用工具时参数要简短完整。"
            "沿用对话里已经成功的工具结果，不重复执行成功操作，不把中止的调用当作成功。"
        )
        current = payload
        for attempt in range(2):
            data = await self._post(url, headers, current)
            choices = data.get("choices") or []
            limited = data.get("stop_reason") == "max_tokens" or any(
                isinstance(choice, dict) and choice.get("finish_reason") == "length"
                for choice in (choices if isinstance(choices, list) else [])
            )
            if not limited:
                return data
            LOGGER.warning("模型输出达到上限：max_tokens=%s attempt=%s", payload['max_tokens'], attempt + 1)
            if attempt:
                raise LLMOutputLimitError("模型压缩重答后仍达到输出上限，未采用不完整结果")
            current = {**payload, "temperature": min(payload.get("temperature", 0.4), 0.4)}
            parts = data.get("content") or []
            has_text = any(isinstance(part, dict) and part.get("type") == "text" and part.get("text")
                           for part in (parts if isinstance(parts, list) else []))
            has_calls = any(isinstance(part, dict) and part.get("type") == "tool_use"
                            for part in (parts if isinstance(parts, list) else []))
            for choice in (choices if isinstance(choices, list) else []):
                message = choice.get("message", {}) if isinstance(choice, dict) else {}
                if isinstance(message, dict):
                    has_text = has_text or bool(message.get("content"))
                    has_calls = has_calls or bool(message.get("tool_calls"))
            if has_text and not has_calls:
                # A clipped final answer only needs rewriting; don't reopen tools.
                current.pop("tools", None)
            if "system" in payload:
                current["system"] = payload["system"] + instruction
            else:
                current["messages"] = [
                    {**message, "content": message["content"] + instruction}
                    if message.get("role") == "system" else message
                    for message in payload["messages"]
                ]
        raise AssertionError("unreachable")

    def _endpoint(self, suffix: str) -> str:
        base = self._settings.llm_base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/{suffix}"
        return f"{base}/v1/{suffix}"

    async def _post(self, url: str, headers: dict, payload: dict, *, timeout_seconds: int = 110) -> dict:
        try:
            # CCR media allows 150 s upstream; image requests must also override
            # the shared session's 120 s default, while ordinary chat stays at 110 s.
            async with asyncio.timeout(timeout_seconds):
                async with self._session.post(url, headers=headers, json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout_seconds, connect=15)) as response:
                    data = await response.json(content_type=None)
                    if response.status == 429:
                        raise LLMError("模型接口限流（HTTP 429），请稍后再试")
                    if response.status >= 400:
                        detail = str(data)[:500]
                        raise LLMError(f"模型接口 HTTP {response.status}: {detail}")
                    if not isinstance(data, dict):
                        raise LLMError("模型接口没有返回 JSON 对象")
                    return data
        except TimeoutError as exc:
            raise LLMError("模型接口请求超时") from exc
        except aiohttp.ClientError as exc:
            raise LLMError(f"模型接口连接失败：{exc}") from exc


def _generated_image_url(data: dict) -> str | None:
    """Extract image URLs from gateway/OpenAI/DashScope-compatible responses."""
    roots = [data]
    output = data.get("output")
    if isinstance(output, dict):
        roots.insert(0, output)

    contents: list[object] = []
    for root in roots:
        choices = root.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                message = choice.get("message")
                if isinstance(message, dict):
                    contents.append(message.get("content"))

        images = root.get("data")
        if isinstance(images, list):
            contents.extend(images)
        results = root.get("results")
        if isinstance(results, list):
            contents.extend(results)

    for content in contents:
        candidate = _image_url_from_content(content)
        if candidate:
            return candidate
    return None


def _image_url_from_content(content: object) -> str | None:
    if isinstance(content, str):
        match = _MARKDOWN_IMAGE_URL_RE.search(content)
        return match.group(0) if match else None
    if isinstance(content, list):
        for part in content:
            candidate = _image_url_from_content(part)
            if candidate:
                return candidate
        return None
    if not isinstance(content, dict):
        return None

    for key in ("image", "url"):
        value = content.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    image_url = content.get("image_url")
    if isinstance(image_url, str) and image_url.startswith("https://"):
        return image_url
    if isinstance(image_url, dict):
        value = image_url.get("url")
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return None
