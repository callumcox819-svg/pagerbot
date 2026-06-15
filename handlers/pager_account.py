"""Connect / disconnect Pager account."""

from __future__ import annotations

import json
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import database as db
from config import load_settings, resolve_pager_org_id
from handlers.states import PagerConnect
from keyboards.main_menu import channels_kb, connect_kb
from services.encryption import Secrets
from services.pager_api import PagerAPIError, PagerClient
from services.pager_auth import authenticate

logger = logging.getLogger(__name__)
router = Router()
_settings = load_settings()
_secrets = Secrets(_settings.encryption_key)


def _pager_client(
    cookies: dict,
    *,
    org_id: str = "",
    org_slug: str = "",
    locale: str = "",
) -> PagerClient:
    slug = org_slug or _settings.pager_org_slug
    resolved_org = resolve_pager_org_id(org_id, _settings.pager_org_id, org_slug=slug)
    return PagerClient(
        _settings.pager_base_url,
        cookies,
        org_id=resolved_org,
        org_slug=slug,
        locale=locale or _settings.pager_locale,
        org_id_fallback=resolved_org,
    )


async def _save_session(tg_user_id: int, email: str, password: str, cookies: dict) -> str:
    org_hint = str(cookies.get("_pager_org_id") or "").strip()
    user_hint = str(cookies.get("_pager_user_id") or "").strip()
    session_enc = _secrets.encrypt(json.dumps(dict(cookies)))
    password_enc = _secrets.encrypt(password) if password else ""
    client = _pager_client(cookies, org_id=org_hint)
    probe = await client.probe_session()
    pager_user_id = probe.get("pager_user_id") or user_hint or ""
    org_slug = probe.get("org_slug") or _settings.pager_org_slug
    account_id = await db.upsert_account(
        tg_user_id,
        email=email,
        password_enc=password_enc,
        session_enc=session_enc,
        org_id=probe.get("org_id") or org_hint,
        org_slug=org_slug,
        pager_locale=_settings.pager_locale,
        pager_user_id=pager_user_id,
        session_ok=1,
        last_error="",
    )
    await db.deactivate_other_accounts(email=email, keep_id=account_id)
    cleared = await db.clear_pauses_for_account(account_id)
    if cleared:
        logger.info("Cleared %s script pauses for account %s", cleared, account_id)
    channels = await client.list_channels_api()
    if channels:
        await db.sync_channels(account_id, channels, default_enabled=False)
    return probe.get("pager_user_id") or "ok"


@router.message(F.text == "🔐 Pager аккаунт")
@router.message(Command("pager"))
async def pager_menu(message: Message) -> None:
    acc = await db.get_account_by_tg(message.from_user.id)
    if acc and acc.get("session_ok"):
        text = (
            f"✅ Pager подключён\n"
            f"Email: <code>{acc.get('email') or '—'}</code>\n"
            f"Org: <code>{acc.get('org_id') or '—'}</code>\n"
            f"Авто-ответ: {'вкл' if acc.get('auto_reply') else 'выкл'}\n"
            f"Пауза: {'да' if acc.get('paused') else 'нет'}"
        )
    else:
        err = (acc or {}).get("last_error") or ""
        text = "Pager не подключён." + (f"\n⚠️ {err}" if err else "")
    await message.answer(text, parse_mode="HTML", reply_markup=connect_kb())


@router.callback_query(F.data == "pager:login")
async def cb_login_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(PagerConnect.email)
    await cb.message.answer("Введите email Pager:")


@router.message(PagerConnect.email)
async def on_email(message: Message, state: FSMContext) -> None:
    await state.update_data(email=(message.text or "").strip())
    await state.set_state(PagerConnect.password)
    await message.answer("Введите пароль Pager (сообщение удалите после входа):")


@router.message(PagerConnect.password)
async def on_password(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    email = data.get("email") or ""
    password = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    status = await message.answer("⏳ Вхожу в Pager… (до 1–2 мин)")
    try:
        auth = await authenticate(email=email, password=password)
        await _save_session(message.from_user.id, email, password, auth["cookies"])
        await status.edit_text(
            "✅ Pager подключён.\n"
            "Откройте 📡 Каналы — включите нужные (по умолчанию все выключены)."
        )
    except Exception as exc:
        logger.exception("login")
        await db.upsert_account(
            message.from_user.id,
            email=email,
            session_ok=0,
            last_error=str(exc)[:500],
        )
        await status.edit_text(
            f"❌ Не удалось войти.\n{exc}\n\n"
            "Попробуйте 🍪 Импорт cookies: скопируйте Cookie из DevTools → Network.",
            parse_mode=None,
        )
    await state.clear()


@router.callback_query(F.data == "pager:cookies")
async def cb_cookies_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(PagerConnect.cookies)
    await cb.message.answer(
        "Как скопировать cookies:\n"
        "1. Откройте pager.co.ua в Chrome (вы уже залогинены)\n"
        "2. F12 → вкладка Network\n"
        "3. Обновите страницу (F5)\n"
        "4. Кликните любой запрос к pager.co.ua\n"
        "5. Request Headers → Cookie → скопируйте всю строку\n\n"
        "Вставьте сюда Cookie целиком\n"
        "или JSON: {\"__session\": \"...\"}",
        parse_mode=None,
    )


@router.message(PagerConnect.cookies)
async def on_cookies(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    status = await message.answer("⏳ Проверяю сессию…")
    try:
        auth = await authenticate(cookie_raw=raw)
        await _save_session(message.from_user.id, "", "", auth["cookies"])
        await status.edit_text("✅ Сессия сохранена. Откройте 📡 Каналы.")
    except Exception as exc:
        await status.edit_text(f"❌ {exc}", parse_mode=None)
    await state.clear()


@router.callback_query(F.data == "pager:disconnect")
async def cb_disconnect(cb: CallbackQuery) -> None:
    await cb.answer()
    await db.delete_account(cb.from_user.id)
    await cb.message.answer("Pager отключён.")


@router.message(F.text == "📡 Каналы")
async def channels_menu(message: Message) -> None:
    acc = await db.get_account_by_tg(message.from_user.id)
    if not acc or not acc.get("session_ok"):
        await message.answer("Сначала подключите Pager: 🔐 Pager аккаунт")
        return
    chs = await db.list_channels(int(acc["id"]))
    if not chs:
        await message.answer(
            "Каналы не найдены. Нажмите 🔄 в меню каналов или переподключите Pager."
        )
        return
    enabled = sum(1 for c in chs if c.get("enabled"))
    hint = f" ({enabled} вкл.)" if enabled else " (все выкл — нажмите чтобы включить)"
    await message.answer(
        f"Каналы{hint} — нажмите чтобы вкл/выкл:",
        reply_markup=channels_kb(chs),
    )


@router.callback_query(F.data.startswith("ch:toggle:"))
async def cb_toggle_channel(cb: CallbackQuery) -> None:
    acc = await db.get_account_by_tg(cb.from_user.id)
    if not acc:
        await cb.answer("Нет аккаунта")
        return
    channel_id = cb.data.split(":", 2)[2]
    chs = await db.list_channels(int(acc["id"]))
    current = next((c for c in chs if c["channel_id"] == channel_id), None)
    enabled = not (current and current.get("enabled"))
    await db.toggle_channel(int(acc["id"]), channel_id, enabled)
    chs = await db.list_channels(int(acc["id"]))
    await cb.message.edit_reply_markup(reply_markup=channels_kb(chs))
    if enabled:
        await cb.answer("Включено — непрочитанные чаты обработаются ~45 сек")
    else:
        await cb.answer("Выключено")


@router.callback_query(F.data == "ch:all_off")
async def cb_all_off_channels(cb: CallbackQuery) -> None:
    acc = await db.get_account_by_tg(cb.from_user.id)
    if not acc:
        await cb.answer("Нет аккаунта")
        return
    await db.disable_all_channels(int(acc["id"]))
    chs = await db.list_channels(int(acc["id"]))
    await cb.message.edit_reply_markup(reply_markup=channels_kb(chs))
    await cb.answer("Все выключены — включите только Kelvin Phiri")


@router.callback_query(F.data == "ch:refresh")
async def cb_refresh_channels(cb: CallbackQuery) -> None:
    acc = await db.get_account_by_tg(cb.from_user.id)
    if not acc or not acc.get("session_enc"):
        await cb.answer("Нет сессии")
        return
    await cb.answer("Обновляю…")
    try:
        cookies = json.loads(_secrets.decrypt(acc["session_enc"]))
        client = _pager_client(
            cookies,
            org_id=str(acc.get("org_id") or ""),
            org_slug=str(acc.get("org_slug") or _settings.pager_org_slug),
            locale=str(acc.get("pager_locale") or ""),
        )
        channels = await client.list_channels_api()
        if client.org_id:
            await db.upsert_account(
                cb.from_user.id,
                org_id=client.org_id,
                org_slug=client.org_slug or acc.get("org_slug") or _settings.pager_org_slug,
                pager_user_id=acc.get("pager_user_id") or "",
                session_ok=1,
            )
        if not channels:
            await cb.message.answer(
                "Каналы не найдены. Проверьте сессию или добавьте "
                "PAGER_ORG_SLUG=tehsup в Railway Variables."
            )
            return
        await db.sync_channels(int(acc["id"]), channels, default_enabled=False)
        chs = await db.list_channels(int(acc["id"]))
        enabled = sum(1 for c in chs if c.get("enabled"))
        hint = f" ({enabled} вкл.)" if enabled else " (все выкл.)"
        await cb.message.edit_text(
            f"Каналы{hint} — {len(chs)} шт. Нажмите чтобы вкл/выкл:",
            reply_markup=channels_kb(chs),
        )
    except PagerAPIError as exc:
        await cb.message.answer(
            f"Ошибка API: {exc}\n\n"
            "Добавьте в Railway Variables:\n"
            "PAGER_ORG_ID=org_… (из Network → status?orgId=…)\n"
            "PAGER_ORG_SLUG=tehsup"
        )
