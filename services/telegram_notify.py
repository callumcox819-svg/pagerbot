"""Send escalation cards to Telegram."""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PAGER_BASE = "https://www.pager.co.ua"


def pager_conversation_url(conv_id: str, base_url: str = PAGER_BASE) -> str:
    """Deep link to a specific Pager chat (opens after login if needed)."""
    root = base_url.rstrip("/")
    cid = (conv_id or "").strip()
    if not cid:
        return f"{root}/chats"
    return f"{root}/chats/{cid}"


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
    pager_base_url: str = PAGER_BASE,
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

    chat_url = pager_conversation_url(conv_id, pager_base_url)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть Pager",
                    url=chat_url,
                )
            ]
        ]
    )
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
