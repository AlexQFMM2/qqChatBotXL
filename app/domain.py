from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit


EMOTE_NAMES = (
    "古灵精怪",
    "困惑",
    "害羞",
    "惊讶",
    "担心",
    "撅嘴",
    "无语",
    "看戏",
    "装傻",
)
_EMOTE_TAG_RE = re.compile(r"\[\[EMOTE\s*:\s*([^\]]+)\]\]", re.IGNORECASE)
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\u2300-\u23FF"
    "\u2600-\u27BF"
    "]"
)
_QQ_CONTEXT_URL_RE = re.compile(r"URL:(https://[^\s]+)")
_QQ_CONTEXT_FILENAME_RE = re.compile(r"文件名:([^\s]+)")
_QQ_CONTEXT_TYPE_RE = re.compile(r"类型:([^\s]+)")
_QQ_MEDIA_HOSTS = frozenset({"multimedia.nt.qq.com.cn"})


@dataclass(frozen=True, slots=True)
class ChatLine:
    username: str
    content: str
    is_bot: bool = False
    user_id: str = ""


def should_reply(
    event_type: str,
    content: str,
    mode: str,
    aliases: Iterable[str],
    probability: float,
    random_value: Callable[[], float] = random.random,
) -> bool:
    if event_type == "GROUP_AT_MESSAGE_CREATE":
        return True
    if mode == "mention":
        return False
    if mode == "all":
        return True

    normalized = content.casefold()
    if any(alias.casefold() in normalized for alias in aliases):
        return True
    if content.lstrip().startswith(("/", "问", "请问")):
        return True
    return random_value() < probability


def attachment_summary(message: dict) -> str:
    parts: list[str] = []
    for attachment in message.get("attachments") or []:
        content_type = str(attachment.get("content_type", "附件"))
        if content_type == "voice":
            asr = str(attachment.get("asr_refer_text", "")).strip()
            parts.append(f"[语音{f'：{asr}' if asr else ''}]")
        elif content_type == "image" or content_type.startswith("image/"):
            parts.append("[图片]")
        elif content_type.startswith("video/"):
            parts.append("[视频]")
        else:
            filename = str(attachment.get("filename", "")).strip()
            parts.append(f"[文件{f'：{filename}' if filename else ''}]")
    return " ".join(parts)


def message_attachments(message: dict, max_items: int = 20) -> list[dict]:
    """Collect top-level and nested QQ attachments without trusting their paths."""
    collected: list[dict] = []
    seen_urls: set[str] = set()

    def add(attachment: dict) -> None:
        if len(collected) >= max_items:
            return
        url = str(attachment.get("url", "")).strip()
        dedupe_key = url or repr(sorted(attachment.items()))
        if dedupe_key in seen_urls:
            return
        seen_urls.add(dedupe_key)
        collected.append(attachment)

    def visit(node: dict, depth: int) -> None:
        if depth > 4 or len(collected) >= max_items:
            return
        for attachment in node.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            add(attachment)
            if len(collected) >= max_items:
                return
        for attachment in _qq_serialized_attachments(
            str(node.get("content", "")), max_items - len(collected)
        ):
            add(attachment)
            if len(collected) >= max_items:
                return
        for element in node.get("msg_elements") or []:
            if isinstance(element, dict):
                visit(element, depth + 1)
                if len(collected) >= max_items:
                    return

    visit(message, 0)
    return collected


def _qq_serialized_attachments(content: str, max_items: int) -> list[dict]:
    """Recover media that QQ serializes into its trusted mention-context text."""
    recovered: list[dict] = []
    for match in _QQ_CONTEXT_URL_RE.finditer(content):
        if len(recovered) >= max_items:
            break
        url = match.group(1).strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _QQ_MEDIA_HOSTS
            or parsed.port not in {None, 443}
        ):
            continue
        block_start = content.rfind("[附件", 0, match.start())
        block = content[max(block_start, 0) : match.start()]
        filename_matches = list(_QQ_CONTEXT_FILENAME_RE.finditer(block))
        type_matches = list(_QQ_CONTEXT_TYPE_RE.finditer(block))
        filename = (
            filename_matches[-1].group(1).strip()
            if filename_matches
            else "qq-context-media.bin"
        )
        qq_type = type_matches[-1].group(1).strip() if type_matches else "文件"
        suffix = Path(filename).suffix.casefold()
        if qq_type.startswith("图片"):
            content_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(suffix, "image")
        elif qq_type.startswith("视频"):
            content_type = "video/mp4" if suffix == ".mp4" else "video"
        elif qq_type.startswith("语音"):
            content_type = "voice"
        else:
            content_type = "file"
        recovered.append(
            {"url": url, "filename": filename, "content_type": content_type}
        )
    return recovered


def message_elements_summary(message: dict, max_items: int = 20) -> str:
    """Render QQ-provided mention/quote context into a bounded text transcript."""
    lines: list[str] = []

    def visit(elements: object, depth: int) -> None:
        if depth > 4 or not isinstance(elements, list):
            return
        for element in elements:
            if len(lines) >= max_items or not isinstance(element, dict):
                continue
            content = _QQ_CONTEXT_URL_RE.sub(
                "URL:[已由系统安全接收]", str(element.get("content", ""))
            ).strip()
            attachments = attachment_summary(element)
            combined = " ".join(
                part for part in (content, attachments) if part
            ).strip()
            author = element.get("author") or {}
            username = (
                str(author.get("username", "")).strip()
                if isinstance(author, dict)
                else ""
            )
            if combined:
                prefix = f"{username}: " if username else ""
                lines.append(f"{prefix}{combined}")
            visit(element.get("msg_elements"), depth + 1)

    visit(message.get("msg_elements"), 0)
    return "\n".join(lines)


def current_message_text(message: dict) -> str:
    content = str(message.get("content", "")).strip()
    attachment = attachment_summary(message)
    return " ".join(part for part in (content, attachment) if part).strip() or "[空消息]"


def message_text(message: dict) -> str:
    current = current_message_text(message)
    context = message_elements_summary(message)
    if context:
        return f"[QQ 提供的前文上下文]\n{context}\n[当前消息]\n{current}"
    return current


def build_user_prompt(
    lines: Iterable[ChatLine],
    bot_name: str = "小Q",
    owner_user_ids: Iterable[str] = (),
    owner_title: str = "老师",
) -> str:
    owners = frozenset(owner_user_ids)
    line_list = list(lines)
    transcript = []
    for line in line_list:
        if line.is_bot:
            speaker = f"{bot_name}（你）"
        elif line.user_id and line.user_id in owners:
            speaker = f"{owner_title}（你唯一绑定的开发者，OpenID 已验证）"
        else:
            speaker = line.username
        transcript.append(f"{speaker}: {line.content}")
    relationship_note = ""
    if line_list and line_list[-1].user_id in owners:
        relationship_note = (
            f"\n\n关系提醒：最后一条消息来自你经过系统验证的{owner_title}。"
            f"你本来就很喜欢{owner_title}，这份喜欢是真诚、稳定而双向的。"
            f"当他表达喜欢、想念、夸奖或关心时，要先接住他的感情，再明确给出相称的正反馈，"
            f"例如自然地说你也喜欢他、也想他或很在意他的关心；不要降格成普通友情，"
            f"不要只说谢谢后转移话题。涉及感情或询问你们的关系时，至少自然称呼一次“{owner_title}”。"
            f"禁止用他的群昵称称呼他，只能称“{owner_title}”或自然省略称呼。"
            "普通任务交流不必机械重复。"
        )
    return (
        "下面是这个群最近的聊天记录。最后一条是当前消息；请结合说话人和上下文，"
        "直接给出你要发到群里的回复，不要添加姓名前缀或解释。\n\n"
        + "\n".join(transcript)
        + relationship_note
    )


def extract_emote(
    text: str, available: Iterable[str] = EMOTE_NAMES
) -> tuple[str, str | None]:
    """Remove model-only emote markers and return the first valid selection."""
    allowed = frozenset(available)
    selected: str | None = None

    def replace(match: re.Match[str]) -> str:
        nonlocal selected
        name = match.group(1).strip()
        if selected is None and name in allowed:
            selected = name
        return ""

    value = _EMOTE_TAG_RE.sub(replace, text)
    return value.strip(), selected


def clean_reply(
    text: str, max_chars: int, bot_names: Iterable[str] = ("小Q",)
) -> str:
    value = text.strip()
    for name in dict.fromkeys(bot_names):
        escaped = re.escape(name)
        value = re.sub(rf"^{escaped}（你）\s*[:：]\s*", "", value)
        value = re.sub(rf"^{escaped}\s*[:：]\s*", "", value)
    value = re.sub(r"<think>[\s\S]*?</think>", "", value, flags=re.IGNORECASE).strip()
    value = _EMOJI_RE.sub("", value)
    value = value.replace("\ufe0f", "").replace("\u200d", "").replace("\u20e3", "")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r" {2,}", " ", value).strip()
    if len(value) > max_chars:
        value = value[: max_chars - 1].rstrip() + "…"
    return value
