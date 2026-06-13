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
from services.worker_loop import start_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


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
    start_worker(bot)
    logger.info("Bot + Pager worker running")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
