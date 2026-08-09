from __future__ import annotations

import base64
import unittest

from image_adapter.server import (
    AdapterError,
    Settings,
    build_upstream_payload,
    extract_image_urls,
)


class AdapterPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            client_api_key="client",
            dashscope_api_key="upstream",
            dashscope_native_url=(
                "https://workspace.cn-beijing.maas.aliyuncs.com/"
                "api/v1/services/aigc/multimodal-generation/generation"
            ),
            allowed_models=frozenset({"qwen-image-edit-max"}),
            max_images=3,
            max_image_bytes=1024,
            request_timeout_seconds=180,
        )

    def test_builds_dashscope_native_multimodal_payload(self) -> None:
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nreference").decode()
        payload = build_upstream_payload(
            {
                "model": "qwen-image-edit-max",
                "prompt": "保持角色一致，改成挥手",
                "images": [{"media_type": "image/png", "data": encoded}],
            },
            self.settings,
        )

        content = payload["input"]["messages"][0]["content"]
        self.assertTrue(content[0]["image"].startswith("data:image/png;base64,"))
        self.assertEqual(content[-1], {"text": "保持角色一致，改成挥手"})
        self.assertFalse(payload["parameters"]["watermark"])

    def test_rejects_unlisted_model_and_invalid_base64(self) -> None:
        with self.assertRaises(AdapterError):
            build_upstream_payload(
                {"model": "other", "prompt": "edit", "images": [{}]},
                self.settings,
            )
        with self.assertRaises(AdapterError):
            build_upstream_payload(
                {
                    "model": "qwen-image-edit-max",
                    "prompt": "edit",
                    "images": [{"media_type": "image/png", "data": "%%%"}],
                },
                self.settings,
            )

    def test_extracts_only_trusted_dashscope_result(self) -> None:
        result = {
            "output": {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "image": (
                                        "https://dashscope-result.oss-cn-beijing."
                                        "aliyuncs.com/edit.png?Expires=1"
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
        }
        self.assertEqual(len(extract_image_urls(result)), 1)


if __name__ == "__main__":
    unittest.main()
