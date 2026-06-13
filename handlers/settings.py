"""Settings and status."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

import database as db
from keyboards.main_menu import main_menu

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Pager AI Bot — Zambia (тест)\n\n"
        "1. 🔐 Pager аккаунт — подключите свой логин\n"
        "2. 📡 Каналы — выберите Kelvin Phiri и др.\n"
        "3. Бот сам шлёт скрипты в Messenger\n"
        "4. В Telegram — только когда нужен человек\n\n"
        "Команды: /start /pause /resume /status",
        reply_markup=main_menu(),
    )


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message) -> None:
    await message.answer(
        "⚙️ Настройки\n"
        "/pause — пауза авто-ответов в Pager\n"
        "/resume — продолжить\n"
        "/escalation — куда слать эскалации (этот чат по умолчанию)"
    )


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    await db.set_account_flags(message.from_user.id, paused=1)
    await message.answer("⏸ Авто-ответы приостановлены.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    await db.set_account_flags(message.from_user.id, paused=0, auto_reply=1)
    await message.answer("▶️ Авто-ответы включены.")


@router.message(Command("escalation"))
async def cmd_escalation(message: Message) -> None:
    await db.set_account_flags(
        message.from_user.id,
        escalation_chat_id=message.chat.id,
    )
    await message.answer(f"Эскалации → этот чат (id {message.chat.id})")


@router.message(F.text == "ℹ️ Статус")
@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    acc = await db.get_account_by_tg(message.from_user.id)
    if not acc:
        await message.answer("Pager не подключён.")
        return
    chs = await db.list_channels(int(acc["id"]))
    enabled_names = [c["name"] for c in chs if c.get("enabled")]
    en = len(enabled_names)
    await message.answer(
        f"Session: {'OK' if acc.get('session_ok') else 'FAIL'}\n"
        f"Org: {acc.get('org_id') or '—'}\n"
        f"Каналов включено: {en}/{len(chs)}\n"
        + (f"Активные: {', '.join(enabled_names)}\n" if enabled_names else "")
        + f"Paused: {bool(acc.get('paused'))}\n"
        f"Auto-reply: {bool(acc.get('auto_reply'))}\n"
        f"Escalation chat: {acc.get('escalation_chat_id') or acc.get('tg_user_id')}\n\n"
        "Обрабатывает канал Kelvin (и др. включённые):\n"
        "• «Без статусу» — новые лиды, шлёт intro/скрипты\n"
        "• Воронка — В процесі / Чекаю ID / Реєстрація\n"
        "Не трогает: Завершено, Депи не дійшли, Скасовано.\n"
        "/reset_pauses — сбросить паузу после эскалаций."
    )


@router.message(Command("reset_pauses"))
async def cmd_reset_pauses(message: Message) -> None:
    acc = await db.get_account_by_tg(message.from_user.id)
    if not acc:
        await message.answer("Pager не подключён.")
        return
    n = await db.clear_pauses_for_account(int(acc["id"]))
    await message.answer(
        f"Сброшено пауз у {n} чат(ов). Бот снова может отвечать в Pager."
    )
