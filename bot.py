"""Pager AI Telegram bot + embedded worker."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import load_settings, resolve_pager_org_id
from database import init_db
from handlers import setup_routers
from services.bot_commands import register_bot_commands
from services.worker_loop import start_worker, _env_truthy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def _ensure_polling_mode(bot: Bot) -> None:
    me = await bot.get_me()
    logger.info(
        "Telegram bot @%s id=%s — Pager AI (ответ: «Pager AI Bot — Zambia»)",
        me.username,
        me.id,
    )
    for attempt in range(5):
        wh = await bot.get_webhook_info()
        if not wh.url:
            logger.info("Telegram polling OK (webhook cleared)")
            return
        logger.warning(
            "Webhook active (%s) — deleteWebhook try %s/5",
            wh.url,
            attempt + 1,
        )
        await bot.delete_webhook(drop_pending_updates=False)
        await asyncio.sleep(1.5)
    wh = await bot.get_webhook_info()
    if wh.url:
        logger.error(
            "Webhook still set (%s). Другой сервис использует этот BOT_TOKEN — "
            "остановите его или создайте новый токен в @BotFather.",
            wh.url,
        )


async def _webhook_guard(bot: Bot) -> None:
    """Another deployment may call setWebhook — keep polling alive."""
    while True:
        await asyncio.sleep(30.0)
        try:
            wh = await bot.get_webhook_info()
            if wh.url:
                logger.warning("Webhook re-set (%s) — clearing for polling", wh.url)
                await bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:
            logger.warning("webhook_guard: %s", exc)


async def main() -> None:
    settings = load_settings()
    await init_db()

    org_ok = resolve_pager_org_id(settings.pager_org_id, org_slug=settings.pager_org_slug)
    logger.info(
        "Pager config: org_id=%s slug=%s",
        org_ok[:12] + "…" if org_ok else "MISSING",
        settings.pager_org_slug or "—",
    )

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(setup_routers())

    await register_bot_commands(bot)
    if _env_truthy("PAGER_RUN_WORKER", default=True):
        start_worker(bot)
        logger.info("Pager worker enabled (PAGER_RUN_WORKER)")
    else:
        logger.info("Pager worker disabled — use worker.py on a separate service")
    await _ensure_polling_mode(bot)
    asyncio.create_task(_webhook_guard(bot))
    logger.info("Bot + Pager worker running")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
