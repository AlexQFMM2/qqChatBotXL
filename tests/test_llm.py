from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.llm import (
    ImageInput,
    VisionInputError,
    _anthropic_user_content,
    _generated_image_url,
    _openai_user_content,
)


class ImageInputTests(unittest.TestCase):
    def test_detects_image_from_magic_bytes_not_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "renamed.bin"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
            image = ImageInput.from_path(path, 100)
        self.assertEqual(image.media_type, "image/png")

    def test_rejects_unsupported_or_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.jpg"
            path.write_bytes(b"not an image")
            with self.assertRaises(VisionInputError):
                ImageInput.from_path(path, 100)
            path.write_bytes(b"\xff\xd8\xff" + b"x" * 20)
            with self.assertRaises(VisionInputError):
                ImageInput.from_path(path, 10)


class VisionPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.image = ImageInput("image/jpeg", b"\xff\xd8\xffdemo")
        self.encoded = base64.b64encode(self.image.data).decode("ascii")

    def test_plain_text_payload_stays_backward_compatible(self) -> None:
        self.assertEqual(_anthropic_user_content("你好", ()), "你好")
        self.assertEqual(_openai_user_content("你好", ()), "你好")

    def test_anthropic_payload_uses_base64_image_block(self) -> None:
        content = _anthropic_user_content("看图", (self.image,))
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(content[0]["source"]["data"], self.encoded)
        self.assertEqual(content[-1], {"type": "text", "text": "看图"})

    def test_openai_payload_uses_data_url(self) -> None:
        content = _openai_user_content("看图", (self.image,))
        self.assertEqual(content[0], {"type": "text", "text": "看图"})
        self.assertEqual(
            content[1]["image_url"]["url"],
            f"data:image/jpeg;base64,{self.encoded}",
        )

class FormatOverrideTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_can_override_anthropic_with_openai_for_vision(self) -> None:
        from app.llm import LLMClient

        settings = SimpleNamespace(llm_api_format="anthropic")
        client = LLMClient(settings, None)
        client._openai = AsyncMock(return_value="看到了")
        client._anthropic = AsyncMock(return_value="不应调用")
        image = ImageInput("image/jpeg", b"\xff\xd8\xffdemo")

        result = await client.complete(
            "system", "user", model="qwen", images=[image], api_format="openai"
        )

        self.assertEqual(result, "看到了")
        client._openai.assert_awaited_once()
        client._anthropic.assert_not_awaited()


class ToolLoopTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _settings() -> SimpleNamespace:
        return SimpleNamespace(
            llm_api_format="anthropic",
            llm_base_url="https://example.test",
            llm_api_key="key",
            llm_model="model",
            llm_max_tokens=200,
            llm_temperature=0.8,
        )

    async def test_tool_limit_forces_final_text_summary(self) -> None:
        from app.llm import LLMClient

        client = LLMClient(self._settings(), None)
        client._post = AsyncMock(
            side_effect=[
                {
                    "content": [
                        {"type": "tool_use", "id": "call-1", "name": "work", "input": {}}
                    ]
                },
                {"content": [{"type": "text", "text": "已完成一部分，并说明了剩余项。"}]},
            ]
        )

        async def execute_tool(name: str, arguments: dict) -> str:
            return "工具执行成功"

        result = await client.complete_with_tools(
            "system",
            "user",
            [{"name": "work", "input_schema": {"type": "object"}}],
            execute_tool,
            max_rounds=1,
        )
        self.assertEqual(result, "已完成一部分，并说明了剩余项。")
        self.assertEqual(client._post.await_count, 2)
        final_payload = client._post.await_args_list[-1].args[2]
        self.assertNotIn("tools", final_payload)
        self.assertIn("工具调用轮次已达到上限", final_payload["messages"][-1]["content"][-1]["text"])

    async def test_plain_anthropic_empty_response_retries_with_larger_budget(self) -> None:
        from app.llm import LLMClient

        client = LLMClient(self._settings(), None)
        client._post = AsyncMock(
            side_effect=[
                {
                    "content": [],
                    "stop_reason": "max_tokens",
                    "usage": {"output_tokens": 200},
                },
                {"content": [{"type": "text", "text": "恢复后的回答"}]},
            ]
        )

        result = await client.complete("system", "user")

        self.assertEqual(result, "恢复后的回答")
        recovery_payload = client._post.await_args_list[-1].args[2]
        self.assertEqual(recovery_payload["max_tokens"], 1600)
        self.assertEqual(recovery_payload["temperature"], 0.4)
        self.assertIn("立即输出", recovery_payload["system"])

    async def test_empty_text_after_tool_gets_no_tool_recovery(self) -> None:
        from app.llm import LLMClient

        client = LLMClient(self._settings(), None)
        client._post = AsyncMock(
            side_effect=[
                {
                    "content": [
                        {"type": "tool_use", "id": "call-1", "name": "work", "input": {}}
                    ]
                },
                {"content": []},
                {"content": [{"type": "text", "text": "这是恢复后的完整回复。"}]},
            ]
        )

        async def execute_tool(name: str, arguments: dict) -> str:
            return "工具执行成功"

        result = await client.complete_with_tools(
            "system",
            "user",
            [{"name": "work", "input_schema": {"type": "object"}}],
            execute_tool,
        )
        self.assertEqual(result, "这是恢复后的完整回复。")
        self.assertEqual(client._post.await_count, 3)
        recovery_payload = client._post.await_args_list[-1].args[2]
        self.assertNotIn("tools", recovery_payload)
        self.assertEqual(recovery_payload["max_tokens"], 1600)
        self.assertIn("不要再调用工具", recovery_payload["messages"][-1]["content"][0]["text"])


class ImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_image_url_from_gateway_response(self) -> None:
        from app.llm import LLMClient

        settings = SimpleNamespace(
            llm_base_url="https://example.test",
            llm_api_key="key",
        )
        client = LLMClient(settings, None)
        client._post = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "image": "https://dashscope-a717.oss-accelerate.aliyuncs.com/result.png"
                                }
                            ]
                        }
                    }
                ]
            }
        )

        result = await client.generate_image("一只猫", model="qwen-image-3.0-pro")

        self.assertTrue(result.endswith("result.png"))
        payload = client._post.await_args.args[2]
        self.assertEqual(payload["model"], "qwen-image-3.0-pro")
        self.assertEqual(payload["messages"][0]["content"][0]["text"], "一只猫")

    async def test_sends_reference_images_to_native_adapter(self) -> None:
        from app.llm import LLMClient

        settings = SimpleNamespace(
            llm_base_url="https://example.test",
            llm_api_key="key",
        )
        client = LLMClient(settings, None)
        client._post = AsyncMock(
            return_value={"data": [{"url": "https://dashscope-result.oss-cn-beijing.aliyuncs.com/edit.png"}]}
        )
        image = ImageInput("image/png", b"\x89PNG\r\n\x1a\nreference")

        result = await client.edit_image(
            "保持角色，改成挥手", model="qwen-image-edit-max", images=[image]
        )

        self.assertTrue(result.endswith("edit.png"))
        url, headers, payload = client._post.await_args.args
        self.assertEqual(url, "https://example.test/v1/image-edits")
        self.assertEqual(headers["Authorization"], "Bearer key")
        self.assertEqual(payload["model"], "qwen-image-edit-max")
        self.assertEqual(payload["prompt"], "保持角色，改成挥手")
        self.assertEqual(payload["images"][0]["media_type"], "image/png")
        self.assertEqual(
            base64.b64decode(payload["images"][0]["data"]), image.data
        )

    def test_extracts_output_wrapped_image_block(self) -> None:
        data = {
            "output": {
                "choices": [
                    {"message": {"content": [{"image": "https://example.test/a.png"}]}}
                ]
            }
        }
        self.assertEqual(_generated_image_url(data), "https://example.test/a.png")

    def test_extracts_markdown_and_openai_image_url(self) -> None:
        markdown = {
            "choices": [
                {"message": {"content": "生成完成：![image](https://example.test/a.png)"}}
            ]
        }
        image_block = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "image_url", "image_url": {"url": "https://example.test/b.png"}}
                        ]
                    }
                }
            ]
        }
        self.assertEqual(_generated_image_url(markdown), "https://example.test/a.png")
        self.assertEqual(_generated_image_url(image_block), "https://example.test/b.png")


if __name__ == "__main__":
    unittest.main()
