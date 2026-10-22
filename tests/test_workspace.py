from __future__ import annotations

import os
import tempfile
import time
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from app.workspace import GroupWorkspace, WorkspaceError


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = GroupWorkspace(
            self.temp.name, max_file_mb=1, max_text_chars=1000, quota_mb=1
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_workbook(self, name: str = "学生信息.xlsx") -> Path:
        path = self.workspace.inbox_path("group-a", name, "message")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "学生表"
        sheet.append(["姓名", "身份证号", "入学日期", "公式", "空列"])
        sheet.append(["小明", "11010120000101001X", date(2026, 9, 1), "=1+1", None])
        second = workbook.create_sheet("说明")
        second["A1"] = "中文说明"
        workbook.save(path)
        workbook.close()
        # openpyxl does not calculate formulas. Inject the cached value that a
        # spreadsheet application would normally store so data_only is tested.
        replacement = path.with_suffix(".cached.xlsx")
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(replacement, "w") as target:
            for entry in source.infolist():
                payload = source.read(entry.filename)
                if entry.filename == "xl/worksheets/sheet1.xml":
                    payload = payload.replace(
                        b"<f>1+1</f><v />", b"<f>1+1</f><v>2</v>"
                    )
                target.writestr(entry, payload)
        os.replace(replacement, path)
        return path

    def test_groups_are_isolated(self) -> None:
        self.workspace.write_text("group-a", "notes/todo.md", "A")
        self.workspace.write_text("group-b", "notes/todo.md", "B")
        self.assertEqual(self.workspace.read_text("group-a", "notes/todo.md"), "A")
        self.assertEqual(self.workspace.read_text("group-b", "notes/todo.md"), "B")
        self.assertNotEqual(
            self.workspace.group_root("group-a"), self.workspace.group_root("group-b")
        )

    def test_parent_traversal_is_rejected(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.workspace.write_text("group-a", "../../escape.txt", "bad")

    def test_absolute_path_is_rejected(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.workspace.read_text("group-a", "/etc/passwd")

    def test_symlink_escape_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        root = self.workspace.group_root("group-a")
        (root / "link.txt").symlink_to(outside)
        with self.assertRaises(WorkspaceError):
            self.workspace.read_text("group-a", "link.txt")

    def test_lists_relative_names_only(self) -> None:
        self.workspace.write_text("group-a", "notes/todo.md", "hello")
        result = self.workspace.list_files("group-a")
        self.assertIn("notes/", result)
        self.assertIn("notes/todo.md (5 B)", result)
        self.assertFalse(any(self.temp.name in item for item in result))

    def test_inbox_filename_is_sanitized_and_deduplicated(self) -> None:
        first = self.workspace.inbox_path("group-a", "../../恶意 文件.txt", "msg-1")
        self.assertEqual(first.name, "恶意_文件.txt")
        first.write_text("one", encoding="utf-8")
        second = self.workspace.inbox_path("group-a", "恶意 文件.txt", "msg-2")
        self.assertNotEqual(first, second)
        self.assertTrue(second.is_relative_to(self.workspace.group_root("group-a")))

    def test_generated_image_path_is_isolated_and_deterministic(self) -> None:
        first = self.workspace.generated_image_path("group-a", "message-1")
        repeated = self.workspace.generated_image_path("group-a", "message-1")
        other_group = self.workspace.generated_image_path("group-b", "message-1")
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other_group)
        self.assertEqual(first.parent.name, "generated")
        self.assertEqual(first.suffix, ".png")

    def test_quota_counts_group_files(self) -> None:
        self.workspace.write_text("group-a", "one.txt", "hello")
        self.assertEqual(self.workspace.usage_bytes("group-a"), 5)
        self.assertEqual(self.workspace.remaining_bytes("group-a"), 1024 * 1024 - 5)

    def test_global_quota_applies_across_groups(self) -> None:
        workspace = GroupWorkspace(
            self.temp.name,
            max_file_mb=2,
            max_text_chars=1000,
            quota_mb=2,
            total_quota_mb=1,
        )
        existing = workspace.inbox_path("group-a", "large.bin", "message")
        existing.write_bytes(b"x" * (1024 * 1024))
        with self.assertRaisesRegex(WorkspaceError, "全局配额"):
            workspace.write_text("group-b", "new.txt", "x")

    def test_cleanup_uses_separate_retention_and_ignores_unmanaged_paths(self) -> None:
        now = time.time()
        old_inbox = self.workspace.inbox_path("group-a", "old.png", "message")
        old_inbox.write_bytes(b"old inbox")
        old_generated = self.workspace.write_text("group-a", "reports/old.txt", "old")
        recent = self.workspace.write_text("group-a", "recent.txt", "recent")
        partial = old_inbox.parent / ".download.png.part"
        partial.write_bytes(b"partial")
        unmanaged = Path(self.temp.name) / "do-not-touch.txt"
        unmanaged.write_text("keep", encoding="utf-8")

        os.utime(old_inbox, (now - 15 * 86400, now - 15 * 86400))
        os.utime(old_generated, (now - 91 * 86400, now - 91 * 86400))
        os.utime(partial, (now - 25 * 3600, now - 25 * 3600))
        os.utime(unmanaged, (now - 365 * 86400, now - 365 * 86400))

        stats = self.workspace.cleanup(14, 90, 24, now=now)
        self.assertEqual(stats.files_removed, 3)
        self.assertFalse(old_inbox.exists())
        self.assertFalse(old_generated.exists())
        self.assertFalse(partial.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unmanaged.exists())

    def test_creates_chinese_pdf_inside_group_workspace(self) -> None:
        path = self.workspace.write_pdf(
            "group-a",
            "练习/九九乘法表.pdf",
            "九九乘法表训练题",
            "姓名：________\n\n2 × 3 = ______\n8 × 9 = ______",
        )
        self.assertTrue(path.is_relative_to(self.workspace.group_root("group-a")))
        payload = path.read_bytes()
        self.assertTrue(payload.startswith(b"%PDF-1.4"))
        self.assertTrue(payload.endswith(b"%%EOF\n"))

    def test_pdf_requires_pdf_extension(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.workspace.write_pdf("group-a", "wrong.txt", "标题", "内容")

    def test_reads_xlsx_sheets_and_typed_cells(self) -> None:
        self.make_workbook()
        value = self.workspace.read_text("group-a", "inbox/学生信息.xlsx")
        self.assertIn("[工作表：学生表，2 行 × 5 列]", value)
        self.assertIn("[工作表：说明，1 行 × 1 列]", value)
        self.assertIn("行号\tA\tB\tC\tD\tE", value)
        self.assertIn("11010120000101001X", value)
        self.assertIn("2026-09-01", value)
        self.assertNotIn("=1+1", value)
        student_row = next(
            line for line in value.splitlines() if "11010120000101001X" in line
        )
        self.assertEqual(student_row.split("\t")[4], "2")
        self.assertEqual(student_row.split("\t")[5], "")

    def test_rejects_legacy_xls_and_corrupt_xlsx(self) -> None:
        legacy = self.workspace.inbox_path("group-a", "legacy.xls", "message")
        legacy.write_bytes(b"legacy")
        with self.assertRaisesRegex(WorkspaceError, "暂不支持旧版"):
            self.workspace.read_text("group-a", "inbox/legacy.xls")

        corrupt = self.workspace.inbox_path("group-a", "corrupt.xlsx", "message")
        corrupt.write_bytes(b"not an xlsx archive")
        with self.assertRaisesRegex(WorkspaceError, "损坏.*加密"):
            self.workspace.read_text("group-a", "inbox/corrupt.xlsx")

    def test_rejects_xlsx_dimension_and_uncompressed_size_limits(self) -> None:
        self.make_workbook("limited.xlsx")
        with patch("app.workspace.XLSX_MAX_ROWS_PER_SHEET", 1):
            with self.assertRaisesRegex(WorkspaceError, "超过 1 行"):
                self.workspace.read_text("group-a", "inbox/limited.xlsx")
        with patch("app.workspace.XLSX_MAX_UNCOMPRESSED_BYTES", 1):
            with self.assertRaisesRegex(WorkspaceError, "解压后超过"):
                self.workspace.read_text("group-a", "inbox/limited.xlsx")



if __name__ == "__main__":
    unittest.main()
