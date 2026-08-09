from __future__ import annotations

import unittest

from app.pdfgen import create_text_pdf


class PdfGeneratorTests(unittest.TestCase):
    def test_generates_multiple_pages_and_cjk_font(self) -> None:
        payload = create_text_pdf("中文标题", "\n".join(f"第 {index} 行" for index in range(100)))
        self.assertTrue(payload.startswith(b"%PDF-1.4"))
        self.assertIn(b"/STSong-Light", payload)
        self.assertIn(b"/W [0 127 500]", payload)
        self.assertIn(b"/Count 3", payload)
        self.assertIn(b"xref", payload)

    def test_replaces_non_bmp_characters(self) -> None:
        payload = create_text_pdf("测试", "表情会安全替换：😀")
        self.assertTrue(payload.endswith(b"%%EOF\n"))


if __name__ == "__main__":
    unittest.main()
