"""Telegram bot command menu (/start in slash picker)."""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand


async def register_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="pager", description="Pager аккаунт"),
            BotCommand(command="status", description="Статус бота"),
            BotCommand(command="pause", description="Пауза авто-ответов"),
            BotCommand(command="resume", description="Включить авто-ответы"),
        ]
    )
