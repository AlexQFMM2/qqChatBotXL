from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .qq import QQClient
from .storage import MemoryStore


LOGGER = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
LAST_GOOD_MORNING_KEY = "scheduled_good_morning_last_date"


class GoodMorningScheduler:
    """Send one proactive greeting per Beijing calendar day."""

    def __init__(
        self, settings: Settings, qq: QQClient, store: MemoryStore
    ) -> None:
        self._settings = settings
        self._qq = qq
        self._store = store
        self.last_success_at: str | None = None
        self.last_error: str | None = None
        self.next_run_at: str | None = None

    def next_target(self, now: datetime | None = None) -> datetime:
        current = (now or datetime.now(BEIJING)).astimezone(BEIJING)
        target = current.replace(
            hour=self._settings.good_morning_hour,
            minute=self._settings.good_morning_minute,
            second=0,
            microsecond=0,
        )
        if target <= current:
            target += timedelta(days=1)
        return target

    async def send_for_date(self, day: date) -> int:
        """Send greetings not already persisted for *day*. Returns sent group count."""
        day_key = day.isoformat()
        owner_mentions = " ".join(
            f'<qqbot-at-user id="{owner_id}" />'
            for owner_id in sorted(self._settings.owner_user_ids)
        )
        content = (
            f"{owner_mentions} {self._settings.owner_title}，早安。"
            "今天也请多指教啦。"
        )
        sent = 0
        for group_id in sorted(self._settings.good_morning_groups):
            if await self._store.get_setting(group_id, LAST_GOOD_MORNING_KEY) == day_key:
                continue
            try:
                await self._qq.send_group_text(group_id, content)
                await self._store.set_setting(group_id, LAST_GOOD_MORNING_KEY, day_key)
                await self._store.add(
                    group_id,
                    "__qqchat_bot__",
                    self._settings.bot_name,
                    content,
                    is_bot=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("向群 %s 发送定时早安失败", group_id)
                continue
            sent += 1
            self.last_success_at = datetime.now(BEIJING).isoformat()
            self.last_error = None
            LOGGER.info("已向群 %s 发送定时早安", group_id)
        return sent

    async def run(self, stop: asyncio.Event) -> None:
        if not self._settings.good_morning_enabled:
            LOGGER.info("定时早安任务未启用")
            return
        while not stop.is_set():
            target = self.next_target()
            self.next_run_at = target.isoformat()
            delay = max(0.0, (target - datetime.now(BEIJING)).total_seconds())
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            # Retry transient QQ/API failures for 15 minutes. Successful groups are
            # skipped on every retry because their date was already persisted.
            retry_deadline = target + timedelta(minutes=15)
            while not stop.is_set():
                await self.send_for_date(target.date())
                pending = any(
                    await self._store.get_setting(group_id, LAST_GOOD_MORNING_KEY)
                    != target.date().isoformat()
                    for group_id in self._settings.good_morning_groups
                )
                if not pending or datetime.now(BEIJING) >= retry_deadline:
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                    return
                except TimeoutError:
                    pass

    def status(self) -> dict[str, str | bool | None]:
        return {
            "enabled": self._settings.good_morning_enabled,
            "timezone": "Asia/Shanghai",
            "time": (
                f"{self._settings.good_morning_hour:02d}:"
                f"{self._settings.good_morning_minute:02d}"
            ),
            "next_run_at": self.next_run_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
        }
