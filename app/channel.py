"""Small channel boundary; persona, model, tools and workspace stay in PersonaBot."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .domain import AttachmentRef


class TransportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    channel: str
    conversation_id: str
    message_id: str
    user_id: str
    username: str
    role: str
    content: str
    command_content: str
    should_answer: bool = True
    attachments: tuple[AttachmentRef, ...] = ()
    bilibili_url: str = ""
    bilibili_include: tuple[str, ...] = ()
    bilibili_media: bool = False


class Transport(Protocol):
    async def send_text(self, conversation_id: str, content: str,
                        message_id: str | None = None, *, sequence: int = 1) -> dict: ...

    async def send_file(self, conversation_id: str, path: Path,
                        message_id: str, *, sequence: int = 1) -> dict: ...

    async def download_attachment(self, reference: str, target: Path, max_bytes: int) -> int: ...


class QQTransport:
    def __init__(self, client):
        self.client = client

    async def send_text(self, *args, **kwargs):
        return await self.client.send_group_text(*args, **kwargs)

    async def send_file(self, *args, **kwargs):
        return await self.client.send_group_file(*args, **kwargs)

    async def download_attachment(self, *args, **kwargs):
        return await self.client.download_attachment(*args, **kwargs)
