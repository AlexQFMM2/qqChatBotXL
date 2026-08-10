from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .domain import strip_qq_context_media


AT_TAG = re.compile(r"<qqbot-at-user\b[^>]*>.*?</qqbot-at-user>|<qqbot-at-user\b[^>]*/?>", re.I | re.S)
ATTACHMENT_MARKER = re.compile(r"\s*\[附件已保存：([^\]]+)\]")


@dataclass(slots=True)
class CleanupPreview:
    attachments: int = 0
    files: int = 0
    messages: int = 0
    scheduled_messages: int = 0
    context_media_messages: int = 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def emote_hashes(directory: Path) -> set[str]:
    return {
        sha256_file(path)
        for path in directory.glob("*.png")
        if path.is_file() and not path.is_symlink()
    }


def sanitize_history_text(content: str, owner_ids: tuple[str, ...]) -> str:
    value = AT_TAG.sub("老师", content)
    for owner_id in sorted(owner_ids, key=len, reverse=True):
        if len(owner_id) >= 6:
            value = value.replace(owner_id, "老师")
    if "[QQ 提供的前文上下文]" in value and (
        "[附件" in value or "URL:[已由系统安全接收]" in value
    ):
        value = strip_qq_context_media(value)
    return value


def run_cleanup(
    database: Path,
    workspace: Path,
    emotes: Path,
    *,
    owner_ids: tuple[str, ...] = (),
    apply: bool = False,
    backup_dir: Path | None = None,
) -> CleanupPreview:
    hashes = emote_hashes(emotes)
    if not hashes:
        raise RuntimeError("没有找到可用于核验的内置表情 PNG")
    connection = sqlite3.connect(database)
    preview = CleanupPreview()
    try:
        rows = connection.execute(
            """
            SELECT a.id, a.chat_message_id, a.group_id, a.relative_path, m.content
            FROM chat_attachments a
            JOIN chat_messages m ON m.id = a.chat_message_id
            WHERE m.is_bot = 0
            """
        ).fetchall()
        polluted: list[tuple[int, int, Path, str, str]] = []
        for attachment_id, message_id, group_id, relative_path, content in rows:
            group_key = hashlib.sha256(str(group_id).encode("utf-8")).hexdigest()[:24]
            path = workspace / group_key / str(relative_path)
            legacy_path = workspace / str(group_id) / str(relative_path)
            if not path.exists() and legacy_path.exists():
                path = legacy_path
            if path.is_symlink() or not path.is_file():
                continue
            if sha256_file(path) in hashes:
                polluted.append((int(attachment_id), int(message_id), path, str(relative_path), str(content)))
        preview.attachments = len(polluted)
        preview.files = len({item[2] for item in polluted})
        preview.messages = len({item[1] for item in polluted})

        text_rows = connection.execute("SELECT id, content FROM chat_messages").fetchall()
        sanitized: list[tuple[str, int]] = []
        for message_id, content in text_rows:
            safe = sanitize_history_text(str(content), owner_ids)
            if safe != content:
                sanitized.append((safe, int(message_id)))
        preview.scheduled_messages = len(sanitized)
        preview.context_media_messages = sum(
            1
            for _message_id, content in text_rows
            if "[QQ 提供的前文上下文]" in str(content)
            and ("[附件" in str(content) or "URL:[已由系统安全接收]" in str(content))
            and strip_qq_context_media(str(content)) != str(content)
        )

        if not apply:
            return preview
        if backup_dir is None:
            raise RuntimeError("正式净化必须指定备份目录")
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"pre-history-cleanup-{time.strftime('%Y%m%d-%H%M%S')}.db"
        backup = sqlite3.connect(target)
        try:
            connection.backup(backup)
        finally:
            backup.close()

        affected_messages: dict[int, tuple[str, set[str]]] = {}
        for attachment_id, message_id, path, relative_path, content in polluted:
            connection.execute("DELETE FROM chat_attachments WHERE id = ?", (attachment_id,))
            previous_content, paths = affected_messages.get(message_id, (content, set()))
            paths.add(relative_path)
            affected_messages[message_id] = (previous_content, paths)
            path.unlink(missing_ok=True)
        for message_id, (content, paths) in affected_messages.items():
            cleaned = ATTACHMENT_MARKER.sub(
                lambda match: "" if match.group(1) in paths else match.group(0), content
            ).strip()
            connection.execute(
                "UPDATE chat_messages SET content = ? WHERE id = ?", (cleaned, message_id)
            )
        connection.executemany(
            "UPDATE chat_messages SET content = ? WHERE id = ?", sanitized
        )
        connection.commit()
        return preview
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="预览或净化 QQ 机器人历史污染")
    parser.add_argument("--database", default=os.getenv("DATABASE_PATH", "/app/data/qqchat.db"))
    parser.add_argument("--workspace", default=os.getenv("WORKSPACE_ROOT", "/workspace"))
    parser.add_argument("--emotes", default=os.getenv("EMOTE_PATH", "/app/emotes"))
    parser.add_argument("--backup-dir", default=os.getenv("DATABASE_BACKUP_DIR", "/app/data/backups"))
    parser.add_argument("--owner-id", action="append", default=[])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    env_ids = tuple(item.strip() for item in os.getenv("OWNER_USER_IDS", "").split(",") if item.strip())
    result = run_cleanup(
        Path(args.database),
        Path(args.workspace),
        Path(args.emotes),
        owner_ids=tuple(args.owner_id) + env_ids,
        apply=args.apply,
        backup_dir=Path(args.backup_dir),
    )
    mode_label = "apply" if args.apply else "dry-run"
    print(
        f"{mode_label}: attachments={result.attachments} files={result.files} "
        f"messages={result.messages} sanitized_messages={result.scheduled_messages} "
        f"context_media_messages={result.context_media_messages}"
    )


if __name__ == "__main__":
    main()
