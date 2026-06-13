"""Standalone worker (optional — worker also runs inside bot.py)."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import load_settings
from database import init_db
from services.worker_loop import start_worker

logging.basicConfig(level=logging.INFO, stream=sys.stdout)


async def main() -> None:
    settings = load_settings()
    await init_db()
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    start_worker(bot)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
