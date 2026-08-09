from __future__ import annotations

import unittest

from app.webtools import (
    WebToolError,
    WebTools,
    format_weather,
    html_to_text,
    normalize_public_url,
    parse_bing_rss,
    validate_public_addresses,
)


class HtmlTextTests(unittest.TestCase):
    def test_hidden_content_is_removed(self) -> None:
        value = html_to_text(
            "<html><head><style>.x{}</style><script>steal()</script></head>"
            "<body><h1>标题</h1><p>正文 <b>内容</b></p></body></html>"
        )
        self.assertIn("标题", value)
        self.assertIn("正文 内容", value)
        self.assertNotIn("steal", value)

    def test_template_article_content_is_kept(self) -> None:
        value = html_to_text("<template><article><h1>角色页</h1><p>正文</p></article></template>")
        self.assertIn("角色页", value)
        self.assertIn("正文", value)


class UrlSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_url_syntax_restrictions(self) -> None:
        with self.assertRaises(WebToolError):
            normalize_public_url("file:///etc/passwd")
        with self.assertRaises(WebToolError):
            normalize_public_url("http://user:pass@example.com/")
        with self.assertRaises(WebToolError):
            normalize_public_url("http://example.com:8080/")
        with self.assertRaises(WebToolError):
            normalize_public_url("http://service.internal/")

    async def test_private_ip_literals_are_blocked_before_request(self) -> None:
        tools = WebTools(timeout_seconds=1)
        for url in ("http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]/"):
            with self.subTest(url=url), self.assertRaises(WebToolError):
                await tools.fetch_url(url)

    def test_mixed_dns_answer_is_rejected(self) -> None:
        with self.assertRaises(WebToolError):
            validate_public_addresses({"93.184.216.34", "192.168.1.10"})


class ParserTests(unittest.TestCase):
    def test_bing_rss_parser_limits_results(self) -> None:
        rss = """<?xml version="1.0"?><rss><channel>
        <item><title>第一条</title><link>https://example.com/1</link><description>摘要一</description></item>
        <item><title>第二条</title><link>https://example.com/2</link><description>摘要二</description></item>
        </channel></rss>"""
        values = parse_bing_rss(rss, 1)
        self.assertEqual(values, [{"title": "第一条", "url": "https://example.com/1", "summary": "摘要一"}])

    def test_weather_formatter(self) -> None:
        value = format_weather(
            {"name": "上海", "admin1": "上海", "country": "中国"},
            {
                "current": {
                    "time": "2026-08-08T20:00",
                    "weather_code": 1,
                    "temperature_2m": 30.2,
                    "apparent_temperature": 35.1,
                    "relative_humidity_2m": 70,
                    "wind_speed_10m": 8.4,
                    "precipitation": 0,
                },
                "daily": {
                    "time": ["2026-08-08"],
                    "weather_code": [2],
                    "temperature_2m_min": [27.0],
                    "temperature_2m_max": [34.0],
                    "precipitation_probability_max": [20],
                },
            },
        )
        self.assertIn("上海", value)
        self.assertIn("大部晴朗", value)
        self.assertIn("27～34°C", value)


if __name__ == "__main__":
    unittest.main()
