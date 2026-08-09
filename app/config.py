from __future__ import annotations

import json
import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"缺少必填环境变量：{name}")
    return value


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


def _csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    qq_app_id: str
    qq_app_secret: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    model_catalog: dict[str, str]
    llm_api_format: str
    llm_max_tokens: int
    llm_temperature: float
    reply_mode: str
    smart_reply_probability: float
    bot_name: str
    bot_aliases: tuple[str, ...]
    owner_user_ids: frozenset[str]
    owner_title: str
    good_morning_enabled: bool
    good_morning_groups: frozenset[str]
    good_morning_hour: int
    good_morning_minute: int
    context_messages: int
    max_reply_chars: int
    task_progress_ack_enabled: bool
    task_queue_workers: int
    task_queue_size: int
    emote_enabled: bool
    emote_path: str
    voice_enabled: bool
    voice_model: str
    voice_max_chars: int
    voice_max_mb: int
    vision_enabled: bool
    vision_model: str | None
    vision_api_format: str | None
    vision_context_messages: int
    vision_max_images: int
    vision_max_image_mb: int
    image_generation_enabled: bool
    image_generation_model: str
    image_generation_max_mb: int
    image_edit_enabled: bool
    image_edit_model: str
    image_edit_max_images: int
    image_character_references: tuple[str, ...]
    image_text_overlay_enabled: bool
    image_text_font_path: str
    agent_access: str
    workspace_root: str
    max_workspace_file_mb: int
    workspace_quota_mb: int
    workspace_total_quota_mb: int
    workspace_inbox_retention_days: int
    workspace_file_retention_days: int
    workspace_part_retention_hours: int
    max_text_file_chars: int
    allowed_groups: frozenset[str]
    database_path: str
    database_backup_dir: str
    database_backup_interval_hours: int
    database_backup_retention_days: int
    persona_path: str
    health_port: int
    webhook_workers: int
    webhook_queue_size: int
    maintenance_interval_minutes: int
    processed_message_retention_days: int
    chat_retention_days: int
    chat_max_messages_per_group: int
    web_tools_enabled: bool
    web_search_results: int
    web_fetch_max_kb: int
    web_fetch_max_chars: int
    web_request_timeout_seconds: float
    log_level: str
    log_message_content: bool
    qq_api_base: str = "https://api.bot.qq.com"
    qq_token_url: str = "https://api.bot.qq.com/app/getAppAccessToken"

    @classmethod
    def from_env(cls) -> "Settings":
        api_format = os.getenv("LLM_API_FORMAT", "anthropic").strip().lower()
        if api_format not in {"anthropic", "openai"}:
            raise ValueError("LLM_API_FORMAT 只能是 anthropic 或 openai")

        raw_vision_api_format = os.getenv("VISION_API_FORMAT", "").strip().lower()
        if raw_vision_api_format not in {"", "anthropic", "openai"}:
            raise ValueError("VISION_API_FORMAT 只能留空、anthropic 或 openai")

        reply_mode = os.getenv("REPLY_MODE", "mention").strip().lower()
        if reply_mode not in {"mention", "smart", "all"}:
            raise ValueError("REPLY_MODE 只能是 mention、smart 或 all")

        agent_access = os.getenv("AGENT_ACCESS", "admins").strip().lower()
        if agent_access not in {"admins", "everyone"}:
            raise ValueError("AGENT_ACCESS 只能是 admins 或 everyone")

        raw_catalog = os.getenv("MODEL_CATALOG_JSON", "").strip()
        try:
            catalog_data = json.loads(raw_catalog) if raw_catalog else {}
        except json.JSONDecodeError as exc:
            raise ValueError("MODEL_CATALOG_JSON 必须是有效 JSON") from exc
        if not isinstance(catalog_data, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in catalog_data.items()
        ):
            raise ValueError("MODEL_CATALOG_JSON 必须是字符串到字符串的 JSON 对象")

        base_url = _required("LLM_BASE_URL").rstrip("/")
        bot_name = os.getenv("BOT_NAME", "夏莉").strip() or "夏莉"
        allowed_groups = frozenset(_csv("ALLOWED_GROUPS"))
        good_morning_groups = frozenset(_csv("GOOD_MORNING_GROUPS")) or allowed_groups
        good_morning_time = os.getenv("GOOD_MORNING_TIME", "07:00").strip()
        try:
            good_morning_hour, good_morning_minute = (
                int(part) for part in good_morning_time.split(":", 1)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("GOOD_MORNING_TIME 必须是 HH:MM 格式") from exc
        if not 0 <= good_morning_hour <= 23 or not 0 <= good_morning_minute <= 59:
            raise ValueError("GOOD_MORNING_TIME 必须是有效的 24 小时时间")

        good_morning_enabled = _bool("GOOD_MORNING_ENABLED", False)
        owner_user_ids = frozenset(_csv("OWNER_USER_IDS"))
        if good_morning_enabled and not good_morning_groups:
            raise ValueError("启用早安任务时必须配置 GOOD_MORNING_GROUPS 或 ALLOWED_GROUPS")
        if good_morning_enabled and not owner_user_ids:
            raise ValueError("启用早安任务时必须配置 OWNER_USER_IDS")
        return cls(
            qq_app_id=_required("QQ_APP_ID"),
            qq_app_secret=_required("QQ_APP_SECRET"),
            llm_base_url=base_url,
            llm_api_key=_required("LLM_API_KEY"),
            llm_model=_required("LLM_MODEL"),
            model_catalog={
                str(key).strip(): str(value).strip()
                for key, value in catalog_data.items()
                if str(key).strip() and str(value).strip()
            },
            llm_api_format=api_format,
            llm_max_tokens=_int("LLM_MAX_TOKENS", 700, 32, 8192),
            llm_temperature=_float("LLM_TEMPERATURE", 0.8, 0.0, 2.0),
            reply_mode=reply_mode,
            smart_reply_probability=_float("SMART_REPLY_PROBABILITY", 0.06, 0.0, 1.0),
            bot_name=bot_name,
            bot_aliases=_csv("BOT_ALIASES") or (
                bot_name,
                "夏莉·沃利克",
                "Shirley",
                "シャーリィ",
            ),
            owner_user_ids=owner_user_ids,
            owner_title=os.getenv("OWNER_TITLE", "老师").strip() or "老师",
            good_morning_enabled=good_morning_enabled,
            good_morning_groups=good_morning_groups,
            good_morning_hour=good_morning_hour,
            good_morning_minute=good_morning_minute,
            context_messages=_int("CONTEXT_MESSAGES", 10, 2, 100),
            max_reply_chars=_int("MAX_REPLY_CHARS", 1500, 50, 4000),
            task_progress_ack_enabled=_bool("TASK_PROGRESS_ACK_ENABLED", True),
            task_queue_workers=_int("TASK_QUEUE_WORKERS", 2, 1, 8),
            task_queue_size=_int("TASK_QUEUE_SIZE", 100, 1, 1000),
            emote_enabled=_bool("EMOTE_ENABLED", True),
            emote_path=os.getenv("EMOTE_PATH", "/app/emotes").strip()
            or "/app/emotes",
            voice_enabled=_bool("VOICE_ENABLED", False),
            voice_model=os.getenv(
                "VOICE_MODEL", "qwen3-tts-vc-2026-01-22"
            ).strip()
            or "qwen3-tts-vc-2026-01-22",
            voice_max_chars=_int("VOICE_MAX_CHARS", 240, 20, 1000),
            voice_max_mb=_int("VOICE_MAX_MB", 8, 1, 20),
            vision_enabled=_bool("VISION_ENABLED", True),
            vision_model=os.getenv("VISION_MODEL", "").strip() or None,
            vision_api_format=raw_vision_api_format or None,
            vision_context_messages=_int("VISION_CONTEXT_MESSAGES", 20, 1, 100),
            vision_max_images=_int("VISION_MAX_IMAGES", 4, 1, 8),
            vision_max_image_mb=_int("VISION_MAX_IMAGE_MB", 8, 1, 20),
            image_generation_enabled=_bool("IMAGE_GENERATION_ENABLED", False),
            image_generation_model=os.getenv(
                "IMAGE_GENERATION_MODEL", "qwen-image-3.0-pro"
            ).strip()
            or "qwen-image-3.0-pro",
            image_generation_max_mb=_int("IMAGE_GENERATION_MAX_MB", 20, 1, 50),
            image_edit_enabled=_bool("IMAGE_EDIT_ENABLED", False),
            image_edit_model=os.getenv(
                "IMAGE_EDIT_MODEL", "qwen-image-edit-max"
            ).strip()
            or "qwen-image-edit-max",
            image_edit_max_images=_int("IMAGE_EDIT_MAX_IMAGES", 3, 1, 4),
            image_character_references=_csv("IMAGE_CHARACTER_REFERENCES")
            or ("害羞.png", "古灵精怪1.png", "担心1.png"),
            image_text_overlay_enabled=_bool("IMAGE_TEXT_OVERLAY_ENABLED", True),
            image_text_font_path=os.getenv(
                "IMAGE_TEXT_FONT_PATH", "/app/fonts/DroidSansFallbackFull.ttf"
            ).strip()
            or "/app/fonts/DroidSansFallbackFull.ttf",
            agent_access=agent_access,
            workspace_root=os.getenv("WORKSPACE_ROOT", "/workspace").strip(),
            max_workspace_file_mb=_int("MAX_WORKSPACE_FILE_MB", 50, 1, 200),
            workspace_quota_mb=_int("WORKSPACE_QUOTA_MB", 500, 10, 10000),
            workspace_total_quota_mb=_int(
                "WORKSPACE_TOTAL_QUOTA_MB", 5120, 100, 100000
            ),
            workspace_inbox_retention_days=_int(
                "WORKSPACE_INBOX_RETENTION_DAYS", 14, 1, 3650
            ),
            workspace_file_retention_days=_int(
                "WORKSPACE_FILE_RETENTION_DAYS", 90, 1, 3650
            ),
            workspace_part_retention_hours=_int(
                "WORKSPACE_PART_RETENTION_HOURS", 24, 1, 720
            ),
            max_text_file_chars=_int("MAX_TEXT_FILE_CHARS", 100000, 1000, 1000000),
            allowed_groups=allowed_groups,
            database_path=os.getenv("DATABASE_PATH", "/app/data/qqchat.db").strip(),
            database_backup_dir=os.getenv(
                "DATABASE_BACKUP_DIR", "/app/data/backups"
            ).strip(),
            database_backup_interval_hours=_int(
                "DATABASE_BACKUP_INTERVAL_HOURS", 24, 1, 720
            ),
            database_backup_retention_days=_int(
                "DATABASE_BACKUP_RETENTION_DAYS", 7, 1, 365
            ),
            persona_path=os.getenv("PERSONA_PATH", "/app/persona.md").strip(),
            health_port=_int("HEALTH_PORT", 8080, 1, 65535),
            webhook_workers=_int("WEBHOOK_WORKERS", 2, 1, 16),
            webhook_queue_size=_int("WEBHOOK_QUEUE_SIZE", 500, 10, 10000),
            maintenance_interval_minutes=_int(
                "MAINTENANCE_INTERVAL_MINUTES", 360, 5, 10080
            ),
            processed_message_retention_days=_int(
                "PROCESSED_MESSAGE_RETENTION_DAYS", 7, 1, 90
            ),
            chat_retention_days=_int("CHAT_RETENTION_DAYS", 30, 1, 3650),
            chat_max_messages_per_group=_int(
                "CHAT_MAX_MESSAGES_PER_GROUP", 1000, 100, 100000
            ),
            web_tools_enabled=_bool("WEB_TOOLS_ENABLED", True),
            web_search_results=_int("WEB_SEARCH_RESULTS", 5, 1, 8),
            web_fetch_max_kb=_int("WEB_FETCH_MAX_KB", 512, 64, 2048),
            web_fetch_max_chars=_int("WEB_FETCH_MAX_CHARS", 20000, 1000, 50000),
            web_request_timeout_seconds=_float(
                "WEB_REQUEST_TIMEOUT_SECONDS", 20, 3, 60
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            log_message_content=_bool("LOG_MESSAGE_CONTENT", False),
        )
