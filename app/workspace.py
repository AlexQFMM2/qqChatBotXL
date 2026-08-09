from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .pdfgen import create_text_pdf


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CleanupStats:
    files_removed: int = 0
    bytes_removed: int = 0
    directories_removed: int = 0


class GroupWorkspace:
    def __init__(
        self,
        root: str,
        max_file_mb: int,
        max_text_chars: int,
        quota_mb: int = 500,
        total_quota_mb: int = 5120,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_mb * 1024 * 1024
        self.quota_bytes = quota_mb * 1024 * 1024
        self.total_quota_bytes = total_quota_mb * 1024 * 1024
        self.max_text_chars = max_text_chars

    def group_root(self, group_id: str) -> Path:
        group_key = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:24]
        path = self.root / group_key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve(self, group_id: str, relative: str, *, must_exist: bool = True) -> Path:
        raw = relative.strip().replace("\\", "/")
        if not raw or PurePosixPath(raw).is_absolute():
            raise WorkspaceError("请提供工作区内的相对路径")
        base = self.group_root(group_id)
        candidate = (base / raw).resolve()
        if not candidate.is_relative_to(base):
            raise WorkspaceError("路径不能离开本群工作区")
        if must_exist and not candidate.exists():
            raise WorkspaceError(f"文件不存在：{raw}")
        return candidate

    def list_files(self, group_id: str, relative: str = ".", limit: int = 80) -> list[str]:
        base = self.group_root(group_id)
        target = base if relative.strip() in {"", "."} else self.resolve(group_id, relative)
        if not target.is_dir():
            raise WorkspaceError("指定路径不是文件夹")
        results: list[str] = []
        for path in sorted(target.rglob("*")):
            if path.is_symlink():
                continue
            rel = path.relative_to(base).as_posix()
            results.append(rel + ("/" if path.is_dir() else f" ({path.stat().st_size} B)"))
            if len(results) >= limit:
                results.append("…列表已截断")
                break
        return results

    def read_text(self, group_id: str, relative: str) -> str:
        path = self.resolve(group_id, relative)
        if not path.is_file():
            raise WorkspaceError("指定路径不是文件")
        if path.stat().st_size > self.max_file_bytes:
            raise WorkspaceError("文件超过工作区读取限制")
        try:
            value = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("这不是 UTF-8 文本文件；可以用 /send 直接发送") from exc
        if len(value) > self.max_text_chars:
            raise WorkspaceError("文本过长，请缩小文件或用 /send 直接发送")
        return value

    def write_text(self, group_id: str, relative: str, content: str) -> Path:
        if len(content) > self.max_text_chars:
            raise WorkspaceError("要写入的文本超过限制")
        path = self.resolve(group_id, relative, must_exist=False)
        encoded_size = len(content.encode("utf-8"))
        existing_size = path.stat().st_size if path.exists() and path.is_file() else 0
        self._ensure_capacity(group_id, encoded_size, existing_size)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_pdf(self, group_id: str, relative: str, title: str, content: str) -> Path:
        if len(content) > self.max_text_chars:
            raise WorkspaceError("要写入 PDF 的文本超过限制")
        path = self.resolve(group_id, relative, must_exist=False)
        if path.suffix.casefold() != ".pdf":
            raise WorkspaceError("PDF 文件名必须以 .pdf 结尾")
        payload = create_text_pdf(title, content)
        if len(payload) > self.max_file_bytes:
            raise WorkspaceError("生成的 PDF 超过文件大小限制")
        existing_size = path.stat().st_size if path.exists() and path.is_file() else 0
        self._ensure_capacity(group_id, len(payload), existing_size)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def file_for_send(self, group_id: str, relative: str) -> Path:
        path = self.resolve(group_id, relative)
        if not path.is_file():
            raise WorkspaceError("指定路径不是文件")
        if path.stat().st_size > self.max_file_bytes:
            raise WorkspaceError("文件超过发送大小限制")
        return path

    def inbox_path(self, group_id: str, filename: str, message_id: str) -> Path:
        """Allocate a safe, non-overwriting path for a QQ attachment."""
        name = Path(filename.replace("\\", "/")).name.strip() or "attachment.bin"
        name = re.sub(r"[^\w.()\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE)
        if name in {"", ".", ".."}:
            name = "attachment.bin"
        inbox = self.group_root(group_id) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        candidate = inbox / name
        if candidate.exists():
            prefix = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:8]
            candidate = inbox / f"{prefix}_{name}"
        return candidate

    def generated_image_path(self, group_id: str, message_id: str) -> Path:
        """Allocate a deterministic path for an image generated for a QQ message."""
        directory = self.group_root(group_id) / "generated"
        directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]
        return directory / f"{name}.png"

    def usage_bytes(self, group_id: str) -> int:
        total = 0
        for path in self.group_root(group_id).rglob("*"):
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        return total

    def remaining_bytes(self, group_id: str) -> int:
        group_remaining = self.quota_bytes - self.usage_bytes(group_id)
        total_remaining = self.total_quota_bytes - self.total_usage_bytes()
        return max(0, min(group_remaining, total_remaining))

    def total_usage_bytes(self) -> int:
        total = 0
        for group_root in self._managed_group_roots():
            for path in group_root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
        return total

    def cleanup(
        self,
        inbox_retention_days: int,
        file_retention_days: int,
        part_retention_hours: int,
        *,
        now: float | None = None,
    ) -> CleanupStats:
        current = time.time() if now is None else now
        inbox_cutoff = current - inbox_retention_days * 24 * 3600
        file_cutoff = current - file_retention_days * 24 * 3600
        part_cutoff = current - part_retention_hours * 3600
        files_removed = 0
        bytes_removed = 0
        directories_removed = 0

        for group_root in self._managed_group_roots():
            for path in list(group_root.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(group_root)
                modified = path.stat().st_mtime
                is_partial = path.name.startswith(".") and path.name.endswith(".part")
                is_inbox = bool(relative.parts) and relative.parts[0] == "inbox"
                expired = (
                    modified < part_cutoff
                    if is_partial
                    else modified < (inbox_cutoff if is_inbox else file_cutoff)
                )
                if not expired:
                    continue
                size = path.stat().st_size
                path.unlink()
                files_removed += 1
                bytes_removed += size

            directories = sorted(
                (path for path in group_root.rglob("*") if path.is_dir() and not path.is_symlink()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in directories:
                try:
                    directory.rmdir()
                    directories_removed += 1
                except OSError:
                    pass

        return CleanupStats(files_removed, bytes_removed, directories_removed)

    def _ensure_capacity(
        self, group_id: str, new_size: int, existing_size: int = 0
    ) -> None:
        if self.usage_bytes(group_id) - existing_size + new_size > self.quota_bytes:
            raise WorkspaceError("本群工作区总配额不足")
        if self.total_usage_bytes() - existing_size + new_size > self.total_quota_bytes:
            raise WorkspaceError("机器人工作区全局配额不足")

    def _managed_group_roots(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [
            path
            for path in self.root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and re.fullmatch(r"[0-9a-f]{24}", path.name)
        ]
