"""Send escalation cards to Telegram."""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


async def notify_escalation(
    bot: Bot,
    chat_id: int,
    *,
    title: str,
    client_name: str,
    channel_name: str,
    folder: str,
    reason: str,
    last_message: str,
    conv_id: str,
    extra: str = "",
) -> None:
    text = (
        f"🚨 <b>{title}</b>\n\n"
        f"👤 {client_name}\n"
        f"📡 {channel_name}\n"
        f"📁 {folder or '—'}\n"
        f"💬 {last_message[:500]}\n"
        f"ℹ️ {reason}\n"
    )
    if extra:
        text += f"\n🆔 {extra}\n"
    text += f"\n<code>{conv_id}</code>"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть Pager",
                    url="https://www.pager.co.ua/",
                )
            ]
        ]
    )
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
