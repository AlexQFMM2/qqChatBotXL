from __future__ import annotations

import asyncio
import logging
import signal
import time

import aiohttp
from aiohttp import web

from .bot import PersonaBot
from .config import Settings
from .llm import LLMClient
from .qq import QQClient
from .scheduler import GoodMorningScheduler
from .storage import MemoryStore
from .webhook import WebhookReceiver
from .webtools import WebTools


async def _maintenance_loop(
    bot: PersonaBot, settings: Settings, stop: asyncio.Event
) -> None:
    logger = logging.getLogger(__name__)
    interval_seconds = settings.maintenance_interval_minutes * 60
    while not stop.is_set():
        try:
            await bot.run_maintenance()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("自动维护执行失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _health(request: web.Request) -> web.Response:
    receiver: WebhookReceiver = request.app["receiver"]
    scheduler: GoodMorningScheduler = request.app["good_morning_scheduler"]
    bot: PersonaBot = request.app["bot"]
    return web.json_response(
        {
            "status": "running",
            "transport": "webhook",
            "webhook_ready": receiver.ready,
            "queue_size": receiver.queue_size,
            "task_queue_size": bot.task_queue_size,
            "task_queue_active": bot.task_queue_active,
            "accepted_events": receiver.accepted_events,
            "rejected_requests": receiver.rejected_requests,
            "last_event_at": receiver.last_event_at,
            "good_morning": scheduler.status(),
            "time": time.time(),
        }
    )


async def run() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)
    store = MemoryStore(settings.database_path)
    timeout = aiohttp.ClientTimeout(total=120, connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        qq = QQClient(settings, session)
        llm = LLMClient(settings, session)
        web_tools = (
            WebTools(
                timeout_seconds=settings.web_request_timeout_seconds,
                max_bytes=settings.web_fetch_max_kb * 1024,
                max_chars=settings.web_fetch_max_chars,
                search_results=settings.web_search_results,
            )
            if settings.web_tools_enabled
            else None
        )
        bot = PersonaBot(settings, qq, llm, store, web_tools)
        good_morning_scheduler = GoodMorningScheduler(settings, qq, store)
        receiver = WebhookReceiver(
            settings.qq_app_id,
            settings.qq_app_secret,
            bot.on_event,
            workers=settings.webhook_workers,
            queue_size=settings.webhook_queue_size,
        )
        await bot.start()
        await receiver.start()

        health_app = web.Application()
        health_app["receiver"] = receiver
        health_app["bot"] = bot
        health_app["good_morning_scheduler"] = good_morning_scheduler
        health_app.router.add_get("/health", _health)
        health_app.router.add_post("/qq/webhook", receiver.handle)
        runner = web.AppRunner(health_app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", settings.health_port)
        await site.start()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        maintenance_task = asyncio.create_task(
            _maintenance_loop(bot, settings, stop), name="qqchat-maintenance"
        )
        good_morning_task = asyncio.create_task(
            good_morning_scheduler.run(stop), name="qqchat-good-morning"
        )

        logger.info(
            "qqchat-bot Webhook 已启动，回复模式：%s，后台任务工人：%s",
            settings.reply_mode,
            settings.task_queue_workers,
        )
        try:
            await stop.wait()
        finally:
            maintenance_task.cancel()
            good_morning_task.cancel()
            await asyncio.gather(
                maintenance_task, good_morning_task, return_exceptions=True
            )
            await receiver.close()
            await bot.close()
            await runner.cleanup()
            store.close()
            logger.info("qqchat-bot 已停止")


def main() -> None:
    try:
        asyncio.run(run())
    except ValueError as exc:
        raise SystemExit(f"配置错误：{exc}") from exc


if __name__ == "__main__":
    main()
