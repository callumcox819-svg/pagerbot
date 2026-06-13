"""Background worker: poll Pager accounts and run script engine."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiogram import Bot

import database as db
from config import load_settings, resolve_pager_org_id
from services.ai_intent import Intent, classify, needs_human
from services.encryption import Secrets
from services.image_extract import extract_id_from_image_url, extract_id_from_text
from services.pager_api import PagerAPIError, PagerClient
from services.script_engine import (
    infer_step_from_history,
    load_script,
    scripts_to_send_after_intent,
)
from services.status_ids import EXCELLENT, ZM_STATUSES, should_skip_processing
from services.telegram_notify import notify_escalation

logger = logging.getLogger(__name__)
_settings = load_settings()
_secrets = Secrets(_settings.encryption_key)

_worker_task: asyncio.Task | None = None


def _cookies(account: dict[str, Any]) -> dict[str, str]:
    raw = account.get("session_enc") or ""
    if not raw:
        return {}
    return json.loads(_secrets.decrypt(raw))


async def _enabled_channel_ids(account_id: int) -> set[str] | None:
    chs = await db.list_channels(account_id)
    if not chs:
        return None
    return {c["channel_id"] for c in chs if c.get("enabled")}


def _is_incoming_direction(value: str) -> bool:
    return (value or "").strip().lower() in ("incoming", "in")


def _is_outgoing_direction(value: str) -> bool:
    return (value or "").strip().lower() in ("outgoing", "out")
    cid = int(account.get("escalation_chat_id") or 0)
    return cid or int(account["tg_user_id"])


def _escalation_link_kwargs(account: dict[str, Any], channel_id: str) -> dict[str, str]:
    return {
        "channel_id": channel_id,
        "org_slug": str(account.get("org_slug") or _settings.pager_org_slug or ""),
        "locale": str(account.get("pager_locale") or _settings.pager_locale or "uk"),
        "pager_base_url": _settings.pager_base_url,
    }


async def _handle_conversation(
    bot: Bot,
    account: dict[str, Any],
    conv: dict[str, Any],
    client: PagerClient,
) -> bool:
    account_id = int(account["id"])
    conv_id = str(conv.get("id") or "")
    if not conv_id:
        return False

    if not _is_incoming_direction(str(conv.get("lastMessageDirection") or "")):
        return False

    enabled = await _enabled_channel_ids(account_id)
    channel_id = str(conv.get("channelId") or "")
    if enabled is None:
        return False
    if not enabled or channel_id not in enabled:
        return False

    if should_skip_processing(conv):
        return False

    state = await db.get_conversation_state(account_id, conv_id)
    if state.get("human_takeover") or state.get("pause_scripts"):
        return False

    messages = await client.list_messages(conv_id, page_size=80)
    # API returns newest first — work chronologically
    msg_only = [m for m in messages if m.get("text") is not None or m.get("attachments")]
    msg_only.sort(key=lambda m: m.get("createdAt") or "")

    last_in = None
    for m in reversed(msg_only):
        if _is_incoming_direction(str(m.get("messageDirection") or "")) and "oldStatusId" not in m:
            last_in = m
            break
    if not last_in:
        return False

    msg_id = str(last_in.get("id") or "")
    if msg_id and msg_id == state.get("last_processed_msg_id"):
        return False

    text = (last_in.get("text") or "").strip()
    attachments = last_in.get("attachments") or []
    has_image = bool(attachments)

    step = infer_step_from_history(msg_only)
    intent = classify(text, has_image=has_image)
    geo = account.get("geo") or "zm"
    pager_user_id = str(account.get("pager_user_id") or "")

    client_name = ((conv.get("client") or {}).get("name") or "Client").strip()
    channel_name = ((conv.get("channel") or {}).get("name") or channel_id).strip()
    folder = ((conv.get("status") or {}) or {}).get("name") or ""

    esc_chat = await _escalation_chat(account)

    # --- Complaints / unclear → TG only ---
    if needs_human(intent, step) and intent != Intent.IMAGE_ONLY:
        await notify_escalation(
            bot,
            esc_chat,
            title="Нужен оператор",
            client_name=client_name,
            channel_name=channel_name,
            folder=folder,
            reason=f"Intent: {intent.value}, step {step}",
            last_message=text or "(photo)",
            conv_id=conv_id,
            **_escalation_link_kwargs(account, channel_id),
        )
        await db.save_conversation_state(
            account_id, conv_id, pause_scripts=1, last_processed_msg_id=msg_id
        )
        return True

    actions_sent = False

    # --- Image: account ID or deposit screenshot ---
    if has_image:
        img_url = ""
        for att in attachments:
            if att.get("type") == "image":
                img_url = (att.get("payload") or {}).get("url") or ""
                break
        extracted = extract_id_from_text(text)
        if not extracted and img_url:
            extracted = await extract_id_from_image_url(img_url, _settings.openai_api_key)

        if step >= 5 and step < 7:
            if extracted:
                await client.send_message(conv_id, EXCELLENT)
                await asyncio.sleep(0.8)
                dep = load_script(geo, "06_deposit")
                await client.send_message(conv_id, dep)
                if pager_user_id:
                    await client.patch_status(conv_id, ZM_STATUSES["registration"], pager_user_id)
                await db.save_conversation_state(
                    account_id,
                    conv_id,
                    step=7,
                    extracted_game_id=extracted,
                    last_processed_msg_id=msg_id,
                )
                await notify_escalation(
                    bot,
                    esc_chat,
                    title="Game ID распознан",
                    client_name=client_name,
                    channel_name=channel_name,
                    folder=folder,
                    reason="Проверьте депозит при необходимости",
                    last_message=text or "(photo)",
                    conv_id=conv_id,
                    extra=f"ID: {extracted}",
                    **_escalation_link_kwargs(account, channel_id),
                )
                return True
            await notify_escalation(
                bot,
                esc_chat,
                title="Фото — ID не распознан",
                client_name=client_name,
                channel_name=channel_name,
                folder=folder,
                reason="Нужен оператор",
                last_message="(photo)",
                conv_id=conv_id,
                **_escalation_link_kwargs(account, channel_id),
            )
            await db.save_conversation_state(
                account_id, conv_id, last_processed_msg_id=msg_id, pause_scripts=1
            )
            return True

        if step >= 7:
            await notify_escalation(
                bot,
                esc_chat,
                title="Скрин депозита",
                client_name=client_name,
                channel_name=channel_name,
                folder=folder,
                reason="Подтвердите депозит вручную",
                last_message="(photo)",
                conv_id=conv_id,
                **_escalation_link_kwargs(account, channel_id),
            )
            if pager_user_id:
                await client.patch_status(conv_id, ZM_STATUSES["deps_pending"], pager_user_id)
            await client.send_message(conv_id, EXCELLENT)
            await asyncio.sleep(0.8)
            await client.send_message(conv_id, load_script(geo, "08_tg_invite"))
            await asyncio.sleep(0.8)
            await client.send_message(conv_id, load_script(geo, "09_tg_link"))
            await db.save_conversation_state(
                account_id, conv_id, step=9, last_processed_msg_id=msg_id
            )
            return True

    # --- Text game ID at wait_id step ---
    if intent == Intent.GAME_ID_TEXT:
        gid = extract_id_from_text(text)
        if gid and step >= 5 and step < 7:
            await client.send_message(conv_id, EXCELLENT)
            await asyncio.sleep(0.8)
            await client.send_message(conv_id, load_script(geo, "06_deposit"))
            if pager_user_id:
                await client.patch_status(conv_id, ZM_STATUSES["registration"], pager_user_id)
            await db.save_conversation_state(
                account_id,
                conv_id,
                step=7,
                extracted_game_id=gid,
                last_processed_msg_id=msg_id,
            )
            return True

    # --- Script chain ---
    keys = scripts_to_send_after_intent(step, intent.value, geo)

    if intent == Intent.READY and step < 4:
        keys = ["04_registration", "05_link"]
    elif intent == Intent.INTERESTED and step < 1:
        keys = ["01_intro"]
    elif intent == Intent.POSITIVE and step < 2:
        keys = ["02_how_it_works", "03_zmw_table"]
    elif intent == Intent.JOINED:
        await db.save_conversation_state(
            account_id, conv_id, step=10, last_processed_msg_id=msg_id
        )
        return True

    for key in keys:
        body = load_script(geo, key)
        await client.send_message(conv_id, body)
        actions_sent = True
        await asyncio.sleep(1.0)

    new_step = step
    if keys == ["04_registration", "05_link"] or (intent == Intent.READY and step < 4):
        new_step = 4
        if pager_user_id:
            await client.patch_status(conv_id, ZM_STATUSES["in_progress"], pager_user_id)
        await asyncio.sleep(0.5)
        await client.send_message(conv_id, load_script(geo, "10_reg_screenshot"))
        if pager_user_id:
            await client.patch_status(conv_id, ZM_STATUSES["wait_id"], pager_user_id)
        new_step = 5
    elif keys == ["01_intro"]:
        new_step = max(new_step, 1)
    elif keys == ["02_how_it_works", "03_zmw_table"]:
        new_step = max(new_step, 2)

    if actions_sent:
        await db.save_conversation_state(
            account_id,
            conv_id,
            step=new_step,
            last_processed_msg_id=msg_id,
        )
        return True
    return False


async def _process_account(bot: Bot, account: dict[str, Any]) -> None:
    try:
        cookies = _cookies(account)
        if not cookies:
            return
        account_id = int(account["id"])
        enabled = await _enabled_channel_ids(account_id)
        if enabled is None:
            return
        if not enabled:
            return

        org_slug = str(
            account.get("org_slug") or _settings.pager_org_slug or ""
        ).strip()
        org_id = resolve_pager_org_id(
            str(account.get("org_id") or ""),
            _settings.pager_org_id,
            org_slug=org_slug,
        )

        client = PagerClient(
            _settings.pager_base_url,
            cookies,
            org_id=org_id,
            org_slug=org_slug,
            locale=str(account.get("pager_locale") or _settings.pager_locale),
            org_id_fallback=org_id,
        )

        seen: set[str] = set()
        inbound = 0
        handled = 0
        for page in (1, 2, 3, 4):
            convs = await client.list_conversations(page=page, page_size=50)
            if not convs:
                break
            for conv in convs:
                if str(conv.get("channelId") or "") not in enabled:
                    continue
                conv_id = str(conv.get("id") or "")
                if not conv_id or conv_id in seen:
                    continue
                seen.add(conv_id)
                if _is_incoming_direction(str(conv.get("lastMessageDirection") or "")):
                    inbound += 1
                try:
                    if await _handle_conversation(bot, account, conv, client):
                        handled += 1
                except Exception:
                    logger.exception(
                        "conv failed account=%s conv=%s",
                        account.get("id"),
                        conv_id,
                    )

        logger.info(
            "Worker account=%s: org=%s channels=%s scanned=%s inbound=%s processed=%s",
            account.get("id"),
            (client.org_id or "")[:16],
            len(enabled),
            len(seen),
            inbound,
            handled,
        )

        if client.org_id:
            await db.upsert_account(
                int(account["tg_user_id"]),
                org_id=client.org_id,
                org_slug=client.org_slug or account.get("org_slug") or _settings.pager_org_slug,
                pager_user_id=account.get("pager_user_id") or "",
                session_ok=1,
            )
    except PagerAPIError as exc:
        if exc.status in (401, 403):
            await db.upsert_account(
                int(account["tg_user_id"]),
                session_ok=0,
                last_error="Session expired — reconnect in bot",
            )
        logger.warning("Pager API account=%s: %s", account.get("id"), exc)


async def worker_loop(bot: Bot) -> None:
    settings = load_settings()
    logger.info(
        "Pager worker started, poll=%ss, org_id=%s, slug=%s",
        settings.poll_sec,
        "ok" if resolve_pager_org_id(settings.pager_org_id, org_slug=settings.pager_org_slug) else "MISSING",
        settings.pager_org_slug or "—",
    )
    while True:
        try:
            accounts = await db.list_worker_accounts()
            for acc in accounts:
                await _process_account(bot, acc)
        except Exception:
            logger.exception("worker tick failed")
        await asyncio.sleep(_settings.poll_sec)


def start_worker(bot: Bot) -> asyncio.Task:
    global _worker_task
    if _worker_task and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(worker_loop(bot))
    return _worker_task
