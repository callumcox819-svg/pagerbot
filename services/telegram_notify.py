"""Send escalation cards to Telegram."""

from __future__ import annotations

from urllib.parse import urlencode

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PAGER_BASE = "https://www.pager.co.ua"


def pager_channel_url(
    channel_id: str,
    *,
    org_slug: str,
    locale: str = "uk",
    base_url: str = PAGER_BASE,
) -> str:
    """Link to Pager inbox for a channel.

    Pager does not expose per-conversation URLs — only channelId in the path
    https://www.pager.co.ua/uk/{orgSlug}/chats?channelId=...
    """
    root = base_url.rstrip("/")
    cid = (channel_id or "").strip()
    slug = (org_slug or "").strip()
    loc = (locale or "uk").strip() or "uk"
    if slug and cid:
        path = f"{root}/{loc}/{slug}/chats"
        return f"{path}?{urlencode({'channelId': cid})}"
    if slug:
        return f"{root}/{loc}/{slug}/chats"
    return f"{root}/{loc}/chats"


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
    channel_id: str = "",
    org_slug: str = "",
    locale: str = "uk",
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
        f"\n🔍 В Pager найди клиента: <b>{client_name}</b>"
    )
    if extra:
        text += f"\n🆔 {extra}\n"
    text += f"\n<code>{conv_id}</code>"

    chat_url = pager_channel_url(
        channel_id,
        org_slug=org_slug,
        locale=locale,
        base_url=pager_base_url,
    )
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
