"""Pager status folder selection (account-wide)."""

from __future__ import annotations

import json
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

import database as db
from config import load_settings
from handlers.pager_account import _pager_client, _secrets
from keyboards.main_menu import folders_kb
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
    client = _pager_client(
        cookies,
        org_id=str(acc.get("org_id") or ""),
        org_slug=str(acc.get("org_slug") or _settings.pager_org_slug),
        locale=str(acc.get("pager_locale") or ""),
    )
    await client.warm_session()
    await client.resolve_org_id_live()
    return client


async def _sync_statuses(acc: dict) -> int:
    client = await _pager_for_account(acc)
    statuses = await client.list_statuses_api()
    if statuses:
        await db.sync_statuses(int(acc["id"]), statuses)
    return len(statuses)


def _folders_text(folder_rows: list[dict], *, synced: int = 0) -> str:
    enabled = sum(1 for r in folder_rows if r.get("enabled"))
    head = f"Папок в Pager: {synced}\n\n" if synced else ""
    return (
        f"{head}"
        "Отметьте <b>папки статусов</b> — откуда бот берёт чаты.\n"
        f"Включено: <b>{enabled}</b>\n\n"
        "Каналы (страницы FB) — в 📡 Каналы.\n"
        "По умолчанию только «Без статусу»."
    )


async def _folder_rows(acc: dict) -> list[dict]:
    return await db.list_account_folder_rows(int(acc["id"]))


@router.message(F.text == "📂 Выбор папок")
async def folders_menu(message: Message) -> None:
    acc = await _require_account(message.from_user.id)
    if not acc:
        await message.answer("Сначала подключите Pager: 🔐 Pager аккаунт")
        return
    try:
        n = await _sync_statuses(acc)
    except PagerAPIError as exc:
        await message.answer(f"❌ Не удалось загрузить папки: {exc}")
        return
    folder_rows = await _folder_rows(acc)
    if not folder_rows or len(folder_rows) <= 1:
        await message.answer(
            "Папки не найдены в Pager. Нажмите 🔄 Обновить папки "
            "или перелогиньтесь в 🔐 Pager аккаунт."
        )
        return
    await message.answer(
        _folders_text(folder_rows, synced=n),
        parse_mode="HTML",
        reply_markup=folders_kb(folder_rows),
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
        folder_rows = await _folder_rows(acc)
        await cb.message.edit_text(
            _folders_text(folder_rows, synced=n),
            parse_mode="HTML",
            reply_markup=folders_kb(folder_rows),
        )
    except PagerAPIError as exc:
        await cb.message.answer(f"❌ {exc}")


@router.callback_query(F.data.startswith("fld:t:"))
async def cb_folder_toggle(cb: CallbackQuery) -> None:
    acc = await _require_account(cb.from_user.id)
    if not acc:
        await cb.answer("Нет сессии")
        return
    try:
        folder_idx = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer("Ошибка")
        return
    account_id = int(acc["id"])
    folder_rows = await _folder_rows(acc)
    if folder_idx < 0 or folder_idx >= len(folder_rows):
        await cb.answer("Папка не найдена")
        return
    row = folder_rows[folder_idx]
    new_state = not bool(row.get("enabled"))
    await db.toggle_account_folder(
        account_id, str(row["status_id"]), new_state
    )
    folder_rows = await _folder_rows(acc)
    await cb.message.edit_text(
        _folders_text(folder_rows),
        parse_mode="HTML",
        reply_markup=folders_kb(folder_rows),
    )
    await cb.answer("Включено" if new_state else "Выключено")


@router.callback_query(F.data == "fld:on")
@router.callback_query(F.data == "fld:off")
async def cb_folder_all(cb: CallbackQuery) -> None:
    acc = await _require_account(cb.from_user.id)
    if not acc:
        await cb.answer("Нет сессии")
        return
    enabled = cb.data == "fld:on"
    account_id = int(acc["id"])
    await db.set_all_account_folders(account_id, enabled)
    folder_rows = await _folder_rows(acc)
    await cb.message.edit_text(
        _folders_text(folder_rows),
        parse_mode="HTML",
        reply_markup=folders_kb(folder_rows),
    )
    await cb.answer("Все включены" if enabled else "Все выключены")
