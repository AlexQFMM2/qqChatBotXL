from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.bot import PersonaBot


class ReplyPrivacyTests(unittest.TestCase):
    def test_redacts_configured_internal_values_and_ip_addresses(self) -> None:
        bot = PersonaBot.__new__(PersonaBot)
        bot._settings = SimpleNamespace(
            qq_app_id="1234567890",
            qq_app_secret="qq-secret-value",
            llm_api_key="sk-private-value",
            llm_base_url="https://internal.example.test",
            qq_api_base="https://api.qq.example.test",
            qq_token_url="https://api.qq.example.test/token",
            owner_user_ids=frozenset({"owner-openid-value"}),
        )

        result = bot._redact_internal_reply(
            "地址 internal.example.test，服务器 203.0.113.8，"
            "key=sk-private-value，老师 owner-openid-value。"
        )

        self.assertNotIn("internal.example.test", result)
        self.assertNotIn("203.0.113.8", result)
        self.assertNotIn("sk-private-value", result)
        self.assertNotIn("owner-openid-value", result)
        self.assertIn("[内部信息已隐藏]", result)

    def test_keeps_public_source_links(self) -> None:
        bot = PersonaBot.__new__(PersonaBot)
        bot._settings = SimpleNamespace(
            qq_app_id="1234567890",
            qq_app_secret="qq-secret-value",
            llm_api_key="private-value",
            llm_base_url="https://internal.example.test",
            qq_api_base="https://api.qq.example.test",
            qq_token_url="https://api.qq.example.test/token",
            owner_user_ids=frozenset(),
        )

        result = bot._redact_internal_reply(
            "公开来源：https://weather.example.org/report"
        )

        self.assertIn("https://weather.example.org/report", result)


if __name__ == "__main__":
    unittest.main()
