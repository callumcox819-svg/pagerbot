"""Per-channel Pager status folder selection."""

from __future__ import annotations

import json
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

import database as db
from config import load_settings
from handlers.pager_account import _pager_client, _secrets
from keyboards.main_menu import folders_channels_kb, folders_toggle_kb
from services.pager_api import PagerAPIError

logger = logging.getLogger(__name__)
router = Router()
_settings = load_settings()


async def _require_account(tg_user_id: int) -> dict | None:
    acc = await db.get_account_by_tg(tg_user_id)
    if not acc or not acc.get("session_ok"):
        return None
    return acc


async def _pager_for_account(acc: dict):
    cookies = json.loads(_secrets.decrypt(acc["session_enc"]))
    return _pager_client(
        cookies,
        org_id=str(acc.get("org_id") or ""),
        org_slug=str(acc.get("org_slug") or _settings.pager_org_slug),
        locale=str(acc.get("pager_locale") or ""),
    )


async def _sync_statuses(acc: dict) -> int:
    client = await _pager_for_account(acc)
    statuses = await client.list_statuses_api()
    if statuses:
        await db.sync_statuses(int(acc["id"]), statuses)
    return len(statuses)


@router.message(F.text == "📂 Выбор папок")
async def folders_menu(message: Message) -> None:
    acc = await _require_account(message.from_user.id)
    if not acc:
        await message.answer("Сначала подключите Pager: 🔐 Pager аккаунт")
        return
    chs = await db.list_channels(int(acc["id"]))
    if not chs:
        await message.answer(
            "Каналы не найдены. Откройте 📡 Каналы → 🔄 Обновить."
        )
        return
    try:
        n = await _sync_statuses(acc)
    except PagerAPIError as exc:
        await message.answer(f"❌ Не удалось загрузить папки: {exc}")
        return
    hint = f"Загружено папок из Pager: {n}.\n" if n else ""
    await message.answer(
        f"{hint}"
        "Выберите канал — отметьте папки, из которых бот берёт чаты.\n"
        "По умолчанию только «Без статусу».",
        reply_markup=folders_channels_kb(chs),
    )


@router.callback_query(F.data == "fld:sync")
async def cb_folders_sync(cb: CallbackQuery) -> None:
    acc = await _require_account(cb.from_user.id)
    if not acc:
        await cb.answer("Нет сессии")
        return
    await cb.answer("Обновляю…")
    try:
        n = await _sync_statuses(acc)
        chs = await db.list_channels(int(acc["id"]))
        await cb.message.edit_text(
            f"Папки обновлены ({n} шт.). Выберите канал:",
            reply_markup=folders_channels_kb(chs),
        )
    except PagerAPIError as exc:
        await cb.message.answer(f"❌ {exc}")


@router.callback_query(F.data == "fld:back")
async def cb_folders_back(cb: CallbackQuery) -> None:
    acc = await _require_account(cb.from_user.id)
    if not acc:
        await cb.answer("Нет сессии")
        return
    chs = await db.list_channels(int(acc["id"]))
    await cb.answer()
    await cb.message.edit_text(
        "Выберите канал — отметьте папки для обработки:",
        reply_markup=folders_channels_kb(chs),
    )


@router.callback_query(F.data.startswith("fld:ch:"))
async def cb_folder_channel(cb: CallbackQuery) -> None:
    acc = await _require_account(cb.from_user.id)
    if not acc:
        await cb.answer("Нет сессии")
        return
    chs = await db.list_channels(int(acc["id"]))
    try:
        ch_idx = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer("Ошибка")
        return
    if ch_idx < 0 or ch_idx >= len(chs):
        await cb.answer("Канал не найден")
        return
    channel = chs[ch_idx]
    account_id = int(acc["id"])
    folder_rows = await db.list_channel_folder_rows(
        account_id, channel["channel_id"]
    )
    enabled = sum(1 for r in folder_rows if r.get("enabled"))
    await cb.answer()
    await cb.message.edit_text(
        f"Канал: <b>{channel.get('name')}</b>\n"
        f"Включено папок: {enabled}\n\n"
        "Нажмите чтобы вкл/выкл:",
        parse_mode="HTML",
        reply_markup=folders_toggle_kb(chs, ch_idx, folder_rows),
    )


@router.callback_query(F.data.startswith("fld:t:"))
async def cb_folder_toggle(cb: CallbackQuery) -> None:
    acc = await _require_account(cb.from_user.id)
    if not acc:
        await cb.answer("Нет сессии")
        return
    parts = cb.data.split(":")
    if len(parts) != 4:
        await cb.answer("Ошибка")
        return
    try:
        ch_idx = int(parts[2])
        folder_idx = int(parts[3])
    except ValueError:
        await cb.answer("Ошибка")
        return
    chs = await db.list_channels(int(acc["id"]))
    if ch_idx < 0 or ch_idx >= len(chs):
        await cb.answer("Канал не найден")
        return
    channel = chs[ch_idx]
    account_id = int(acc["id"])
    folder_rows = await db.list_channel_folder_rows(
        account_id, channel["channel_id"]
    )
    if folder_idx < 0 or folder_idx >= len(folder_rows):
        await cb.answer("Папка не найдена")
        return
    row = folder_rows[folder_idx]
    new_state = not bool(row.get("enabled"))
    await db.toggle_channel_folder(
        account_id, channel["channel_id"], str(row["status_id"]), new_state
    )
    folder_rows = await db.list_channel_folder_rows(
        account_id, channel["channel_id"]
    )
    enabled = sum(1 for r in folder_rows if r.get("enabled"))
    await cb.message.edit_text(
        f"Канал: <b>{channel.get('name')}</b>\n"
        f"Включено папок: {enabled}\n\n"
        "Нажмите чтобы вкл/выкл:",
        parse_mode="HTML",
        reply_markup=folders_toggle_kb(chs, ch_idx, folder_rows),
    )
    await cb.answer("Включено" if new_state else "Выключено")


@router.callback_query(F.data.startswith("fld:on:") | F.data.startswith("fld:off:"))
async def cb_folder_all(cb: CallbackQuery) -> None:
    acc = await _require_account(cb.from_user.id)
    if not acc:
        await cb.answer("Нет сессии")
        return
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer("Ошибка")
        return
    try:
        ch_idx = int(parts[2])
    except ValueError:
        await cb.answer("Ошибка")
        return
    enabled = parts[1] == "on"
    chs = await db.list_channels(int(acc["id"]))
    if ch_idx < 0 or ch_idx >= len(chs):
        await cb.answer("Канал не найден")
        return
    channel = chs[ch_idx]
    account_id = int(acc["id"])
    await db.set_all_channel_folders(
        account_id, channel["channel_id"], enabled
    )
    folder_rows = await db.list_channel_folder_rows(
        account_id, channel["channel_id"]
    )
    count = sum(1 for r in folder_rows if r.get("enabled"))
    await cb.message.edit_text(
        f"Канал: <b>{channel.get('name')}</b>\n"
        f"Включено папок: {count}\n\n"
        "Нажмите чтобы вкл/выкл:",
        parse_mode="HTML",
        reply_markup=folders_toggle_kb(chs, ch_idx, folder_rows),
    )
    await cb.answer("Все включены" if enabled else "Все выключены")
