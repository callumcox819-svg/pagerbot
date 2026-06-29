"""Settings and status."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

import database as db
from keyboards.main_menu import main_menu
from services.llm_client import llm_router_mode
from services.llm_learn import format_learn_feedback

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Pager AI Bot — Zambia (тест)\n\n"
        "1. 🔐 Pager аккаунт — подключите свой логин\n"
        "2. 📡 Каналы — выберите Kelvin Phiri и др.\n"
        "3. Бот сам шлёт скрипты в Messenger\n"
        "4. В Telegram — только когда нужен человек\n\n"
        "Команды: /start /pause /resume /status /learn_stats",
        reply_markup=main_menu(),
    )


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message) -> None:
    await message.answer(
        "⚙️ Настройки\n"
        "/pause — пауза авто-ответов в Pager\n"
        "/resume — продолжить\n"
        "/learn_stats — отчёт обучения AI\n"
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
        f"LLM learn: {llm_router_mode() or 'off'}\n"
        f"Escalation chat: {acc.get('escalation_chat_id') or acc.get('tg_user_id')}\n\n"
        "Обрабатывает канал Kelvin (и др. включённые):\n"
        "• «Без статусу» — новые лиды, шлёт intro/скрипты\n"
        "• Воронка — В процесі / Чекаю ID / Реєстрація\n"
        "Не трогает: Завершено, Депи не дійшли, Скасовано.\n"
        "/reset_pauses — сбросить паузу после эскалаций.\n"
        "/learn_stats — что AI уже выучил из Завершено и Депи не дошли."
    )


@router.message(Command("learn_stats"))
async def cmd_learn_stats(message: Message) -> None:
    acc = await db.get_account_by_tg(message.from_user.id)
    if not acc:
        await message.answer("Pager не подключён. Сначала 🔐 Pager аккаунт.")
        return
    text = await format_learn_feedback(
        int(acc["id"]),
        email=str(acc.get("email") or ""),
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("reset_pauses"))
async def cmd_reset_pauses(message: Message) -> None:
    acc = await db.get_account_by_tg(message.from_user.id)
    if not acc:
        await message.answer("Pager не подключён.")
        return
    aid = int(acc["id"])
    await db.set_account_flags(message.from_user.id, paused=0, auto_reply=1)
    n = await db.clear_pauses_for_account(aid)
    deleted = await db.reset_conversation_states(aid)
    await message.answer(
        f"▶️ Авто-ответы включены.\n"
        f"Сброшено: паузы/метки у {n} чат(ов), полный reset {deleted} записей.\n"
        f"Бот обработает «Без статусу» (~8 чатов за цикл, take chat + intro)."
    )
