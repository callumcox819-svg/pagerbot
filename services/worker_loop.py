"""Background worker: poll Pager accounts and run script engine."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiogram import Bot

import database as db
from config import load_settings, resolve_pager_org_id, resolve_operator_user_id
from services.ai_intent import Intent, classify, needs_human
from services.encryption import Secrets
from services.image_extract import extract_id_from_image_url, extract_id_from_text
from services.pager_api import PagerAPIError, PagerClient, is_session_error
from services.pager_browser_send import send_batch_via_browser
from services.session_refresh import refresh_pager_session
from services.script_engine import (
    infer_step_from_history,
    load_script,
    scripts_to_resend_for_step,
    scripts_to_send_after_intent,
)
from services.status_ids import EXCELLENT, ZM_STATUSES, is_no_status, should_process_conversation
from services.telegram_notify import notify_escalation

logger = logging.getLogger(__name__)
_settings = load_settings()
_secrets = Secrets(_settings.encryption_key)

_worker_task: asyncio.Task | None = None


class _CycleSendBuffer:
    """Queue outbound texts for one Playwright login per worker cycle."""

    def __init__(
        self,
        account: dict[str, Any],
        *,
        org_id: str,
        org_slug: str,
        locale: str,
        pager_user_id: str,
        client: PagerClient,
    ) -> None:
        self.account = account
        self.org_id = org_id
        self.org_slug = org_slug
        self.locale = locale
        self.pager_user_id = pager_user_id
        self.client = client
        self._jobs: dict[str, list[str]] = {}
        self._clients: dict[str, str] = {}
        self._commits: list[tuple[str, dict[str, Any]]] = []

    def queue_send(
        self, conv_id: str, texts: list[str], *, client_name: str = ""
    ) -> None:
        bodies = [t.strip() for t in texts if (t or "").strip()]
        if not bodies:
            return
        bucket = self._jobs.setdefault(conv_id, [])
        bucket.extend(bodies)
        if client_name:
            self._clients[conv_id] = client_name.strip()

    def queue_commit(self, conv_id: str, **kwargs: Any) -> None:
        for i, (cid, fields) in enumerate(self._commits):
            if cid == conv_id:
                self._commits[i] = (cid, {**fields, **kwargs})
                return
        self._commits.append((conv_id, dict(kwargs)))

    async def flush(self) -> set[str]:
        if not self._jobs:
            return set()

        email = str(self.account.get("email") or "").strip()
        pwd_enc = str(self.account.get("password_enc") or "").strip()
        password = _secrets.decrypt(pwd_enc) if pwd_enc else ""
        if not email or not password:
            raise RuntimeError(
                "Account email/password required — reconnect Pager in Telegram bot"
            )

        jobs = [
            (cid, texts, self._clients.get(cid, ""))
            for cid, texts in self._jobs.items()
        ]
        timeout = min(540.0, 90.0 + 75.0 * len(jobs))
        logger.info(
            "browser batch flush jobs=%s texts=%s",
            len(jobs),
            sum(len(job[1]) for job in jobs),
        )
        ok, fresh_cookies = await asyncio.wait_for(
            send_batch_via_browser(
                jobs,
                org_id=self.org_id,
                org_slug=self.org_slug,
                user_id=self.pager_user_id,
                locale=self.locale,
                email=email,
                password=password,
            ),
            timeout=timeout,
        )
        if fresh_cookies:
            self.client.cookies.update(fresh_cookies)
            try:
                await db.upsert_account(
                    int(self.account["tg_user_id"]),
                    session_enc=_secrets.encrypt(json.dumps(self.client.cookies)),
                    session_ok=1,
                )
            except Exception:
                pass

        account_id = int(self.account["id"])
        uid = self.pager_user_id
        for conv_id, fields in self._commits:
            if conv_id in ok:
                try:
                    await self.client.mark_conversation_read(
                        conv_id, user_id=uid
                    )
                except Exception:
                    pass
                await db.save_conversation_state(
                    account_id, conv_id, send_failures=0, **fields
                )
            else:
                st = await db.get_conversation_state(account_id, conv_id)
                fails = int(st.get("send_failures") or 0) + 1
                patch: dict[str, Any] = {"send_failures": fails}
                if fails >= 5:
                    patch["pause_scripts"] = 1
                await db.save_conversation_state(account_id, conv_id, **patch)

        self._jobs.clear()
        self._clients.clear()
        self._commits.clear()
        return ok


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


def _escalation_chat(account: dict[str, Any]) -> int:
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
    send_buf: _CycleSendBuffer,
) -> bool | str:
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

    if not should_process_conversation(conv):
        return False

    state = await db.get_conversation_state(account_id, conv_id)
    if state.get("human_takeover") or state.get("pause_scripts"):
        logger.info(
            "conv=%s skipped paused=%s human=%s",
            conv_id[:8],
            bool(state.get("pause_scripts")),
            bool(state.get("human_takeover")),
        )
        return "paused"
    if int(state.get("send_failures") or 0) >= 5:
        logger.info(
            "conv=%s skipped send_failures=%s (use /reset_pauses)",
            conv_id[:8],
            state.get("send_failures"),
        )
        return "paused"

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
    org_slug = str(
        account.get("org_slug") or _settings.pager_org_slug or ""
    ).strip()
    pager_user_id = resolve_operator_user_id(
        _settings.pager_user_id,
        org_slug=org_slug,
    )

    def _valid_outgoing_reply(m: dict[str, Any]) -> bool:
        if not _is_outgoing_direction(str(m.get("messageDirection") or "")):
            return False
        if "oldStatusId" in m or "oldResponsibleId" in m:
            return False
        if not (m.get("text") or m.get("attachments")):
            return False
        author = str(m.get("authorId") or "").strip()
        if pager_user_id:
            if author != pager_user_id:
                return False
        elif not author:
            return False
        return bool(m.get("isDelivered") or m.get("facebookMessageId"))

    def _has_operator_reply_after(last_in_ts: str) -> bool:
        return any(
            _valid_outgoing_reply(m)
            and str(m.get("createdAt") or "") > last_in_ts
            for m in msg_only
        )

    last_in_ts = str(last_in.get("createdAt") or "")
    needs_reply = not _has_operator_reply_after(last_in_ts)
    is_retry = False
    if msg_id and msg_id == state.get("last_processed_msg_id"):
        if not needs_reply:
            return "done"
        logger.info(
            "conv=%s retry: was marked processed but no reply sent",
            conv_id[:8],
        )
        is_retry = True

    text = (last_in.get("text") or "").strip()
    attachments = last_in.get("attachments") or []
    has_image = bool(attachments)
    has_ad = bool(last_in.get("adId") or last_in.get("adUrl"))

    hist_step = infer_step_from_history(msg_only, pager_user_id)
    if needs_reply:
        step = hist_step
    else:
        step = max(int(state.get("step") or 0), hist_step)

    intent = classify(text, has_image=has_image, has_ad=has_ad)
    geo = account.get("geo") or "zm"
    no_status = is_no_status(conv)

    logger.info(
        "conv=%s intent=%s step=%s retry=%s text=%r",
        conv_id[:8],
        intent.value,
        step,
        is_retry,
        (text or "(photo)")[:40],
    )

    client_name = ((conv.get("client") or {}).get("name") or "Client").strip()
    channel_name = ((conv.get("channel") or {}).get("name") or channel_id).strip()
    folder = ((conv.get("status") or {}) or {}).get("name") or ""

    esc_chat = _escalation_chat(account)

    async def _outbound_send(texts: list[str]) -> None:
        send_buf.queue_send(conv_id, texts, client_name=client_name)

    async def send(text: str) -> None:
        send_buf.queue_send(conv_id, [text], client_name=client_name)

    # Waiting for game ID / deposit photo — ignore short acks once operator replied.
    if (
        step >= 5
        and intent in (Intent.UNKNOWN, Intent.QUESTION, Intent.POSITIVE)
        and not has_image
        and not needs_reply
    ):
        await db.save_conversation_state(
            account_id, conv_id, last_processed_msg_id=msg_id
        )
        return "done"

    # --- Complaints / unclear → TG only ---
    if needs_human(intent, step, no_status=no_status) and intent != Intent.IMAGE_ONLY:
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
                await _outbound_send([EXCELLENT, load_script(geo, "06_deposit")])
                if pager_user_id:
                    await client.patch_status(
                        conv_id, ZM_STATUSES["registration"], pager_user_id
                    )
                send_buf.queue_commit(
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
            await _outbound_send(
                [
                    EXCELLENT,
                    load_script(geo, "08_tg_invite"),
                    load_script(geo, "09_tg_link"),
                ]
            )
            if pager_user_id:
                await client.patch_status(
                    conv_id, ZM_STATUSES["deps_pending"], pager_user_id
                )
            send_buf.queue_commit(
                conv_id, step=9, last_processed_msg_id=msg_id
            )
            return True

    # --- Text game ID at wait_id step ---
    if intent == Intent.GAME_ID_TEXT:
        gid = extract_id_from_text(text)
        if gid and step >= 5 and step < 7:
            await _outbound_send([EXCELLENT, load_script(geo, "06_deposit")])
            if pager_user_id:
                await client.patch_status(
                    conv_id, ZM_STATUSES["registration"], pager_user_id
                )
            send_buf.queue_commit(
                conv_id,
                step=7,
                extracted_game_id=gid,
                last_processed_msg_id=msg_id,
            )
            return True

    # --- Script chain (strict funnel order) ---
    keys: list[str] = []

    if no_status and needs_reply:
        if hist_step < 1:
            keys = ["01_intro"]
        elif hist_step < 2:
            keys = ["02_how_it_works", "03_zmw_table"]
        elif hist_step < 4:
            keys = ["04_registration", "05_link"]
        elif hist_step < 5:
            keys = ["10_reg_screenshot"]
        else:
            keys = scripts_to_resend_for_step(hist_step)
        if keys:
            logger.info(
                "conv=%s no_status hist_step=%s keys=%s",
                conv_id[:8],
                hist_step,
                keys,
            )
    else:
        keys = scripts_to_send_after_intent(step, intent.value, geo)

        if intent == Intent.INTERESTED and step < 1:
            keys = ["01_intro"]
        elif intent in (Intent.POSITIVE, Intent.INTERESTED) and step < 2:
            keys = ["02_how_it_works", "03_zmw_table"]
        elif intent in (Intent.POSITIVE, Intent.READY) and step >= 2 and step < 4:
            keys = ["04_registration", "05_link"]
        elif needs_reply and not keys:
            keys = scripts_to_resend_for_step(hist_step)
            if keys:
                logger.info(
                    "conv=%s resend hist_step=%s keys=%s",
                    conv_id[:8],
                    hist_step,
                    keys,
                )

    if intent == Intent.JOINED:
        await db.save_conversation_state(
            account_id, conv_id, step=10, last_processed_msg_id=msg_id
        )
        return True

    bodies: list[str] = []
    for key in keys:
        if keys == ["04_registration", "05_link"] and key == "05_link":
            continue
        if keys == ["04_registration", "05_link"] and key == "04_registration":
            body = (
                load_script(geo, "04_registration")
                + "\n\n"
                + load_script(geo, "05_link")
            )
        else:
            body = load_script(geo, key)
        bodies.append(body)

    if bodies:
        logger.info(
            "conv=%s sending %s script(s) keys=%s",
            conv_id[:8],
            len(bodies),
            keys,
        )
        await _outbound_send(bodies)
        actions_sent = True

    new_step = step
    if keys == ["04_registration", "05_link"] or (
        intent == Intent.READY and step >= 2 and step < 4
    ):
        new_step = 4
        await send(load_script(geo, "10_reg_screenshot"))
        if pager_user_id:
            await client.patch_status(
                conv_id, ZM_STATUSES["in_progress"], pager_user_id
            )
            await asyncio.sleep(0.5)
            await client.patch_status(
                conv_id, ZM_STATUSES["wait_id"], pager_user_id
            )
        new_step = 5
    elif keys == ["01_intro"]:
        new_step = max(new_step, 1)
    elif keys == ["02_how_it_works", "03_zmw_table"]:
        new_step = max(new_step, 2)

    if actions_sent:
        send_buf.queue_commit(
            conv_id,
            step=new_step,
            last_processed_msg_id=msg_id,
        )
        return True
    if keys:
        logger.warning(
            "conv=%s intent=%s step=%s keys=%s but nothing sent",
            conv_id[:8],
            intent.value,
            step,
            keys,
        )
    return "no_script"


async def _ensure_enabled_channels(
    account_id: int, client: PagerClient
) -> set[str] | None:
    """Return enabled channel ids; sync from API and auto-enable if none on."""
    chs = await db.list_channels(account_id)
    if not chs:
        try:
            api_chs = await client.list_channels_api()
        except Exception as exc:
            logger.warning(
                "Worker account=%s: channel sync failed: %s",
                account_id,
                exc,
            )
            return None
        if not api_chs:
            return None
        await db.sync_channels(account_id, api_chs, default_enabled=True)
        return {c["channel_id"] for c in api_chs}

    enabled = {c["channel_id"] for c in chs if c.get("enabled")}
    if not enabled:
        n = await db.enable_all_channels(account_id)
        logger.info(
            "Worker account=%s: auto-enabled %s channel(s)",
            account_id,
            n,
        )
        return {c["channel_id"] for c in chs}
    return enabled


async def _process_account(bot: Bot, account: dict[str, Any]) -> None:
    try:
        account_id = int(account["id"])
        cookies = _cookies(account)
        if not cookies:
            logger.warning(
                "Worker account=%s: no cookies — refreshing session",
                account_id,
            )
            fresh = await refresh_pager_session(account)
            if not fresh:
                return
            acc = await db.get_account_by_tg(int(account["tg_user_id"]))
            if acc:
                account.update(acc)
            cookies = _cookies(account)

        org_slug = str(
            account.get("org_slug") or _settings.pager_org_slug or ""
        ).strip()
        org_id = resolve_pager_org_id(
            str(account.get("org_id") or ""),
            _settings.pager_org_id,
            org_slug=org_slug,
        )

        def _make_client(cookie_dict: dict[str, str]) -> PagerClient:
            session_uid = resolve_operator_user_id(
                _settings.pager_user_id,
                account.get("pager_user_id"),
                org_slug=org_slug,
            )
            return PagerClient(
                _settings.pager_base_url,
                cookie_dict,
                org_id=org_id,
                org_slug=org_slug,
                locale=str(account.get("pager_locale") or _settings.pager_locale),
                org_id_fallback=org_id,
                session_user_id=session_uid,
            )

        client = _make_client(cookies)
        try:
            probe = await client.probe_session()
        except PagerAPIError:
            probe = None
        if not probe:
            logger.warning(
                "Worker account=%s: session stale — refreshing",
                account_id,
            )
            fresh = await refresh_pager_session(account)
            if not fresh:
                return
            acc = await db.get_account_by_tg(int(account["tg_user_id"]))
            if acc:
                account.update(acc)
            client = _make_client(fresh)

        enabled = await _ensure_enabled_channels(account_id, client)
        if enabled is None:
            logger.warning(
                "Worker account=%s: no channels — refresh in Telegram bot",
                account_id,
            )
            return
        if not enabled:
            logger.warning(
                "Worker account=%s: no enabled channels",
                account_id,
            )
            return
        await client.warm_session()
        if not client.session_user_id:
            uid = await client.resolve_session_user_id()
            if not uid and _settings.pager_user_id:
                client.session_user_id = _settings.pager_user_id

        try:
            convs = await client.collect_conversations(enabled, max_pages=5)
        except PagerAPIError as exc:
            if not is_session_error(exc):
                raise
            logger.warning(
                "Session stale account=%s, refreshing…",
                account.get("id"),
            )
            fresh = await refresh_pager_session(account)
            if not fresh:
                return
            acc = await db.get_account_by_tg(int(account["tg_user_id"]))
            if acc:
                account.update(acc)
            client = _make_client(fresh)
            await client.warm_session()
            convs = await client.collect_conversations(enabled, max_pages=5)
        inbound_convs = [
            c
            for c in convs
            if _is_incoming_direction(str(c.get("lastMessageDirection") or ""))
        ]

        no_status_count = sum(1 for c in inbound_convs if is_no_status(c))
        if inbound_convs:
            logger.info(
                "Worker account=%s: inbound=%s no_status=%s funnel=%s",
                account.get("id"),
                len(inbound_convs),
                no_status_count,
                len(inbound_convs) - no_status_count,
            )

        no_status_inbound = [c for c in inbound_convs if is_no_status(c)]
        if no_status_inbound:
            logger.info(
                "Worker account=%s: backlog mode — only «Без статусу» (%s chats)",
                account.get("id"),
                len(no_status_inbound),
            )
            inbound_convs = no_status_inbound

        async def _priority(conv: dict) -> tuple[int, str]:
            cid = str(conv.get("id") or "")
            st = await db.get_conversation_state(account_id, cid)
            ts = str(conv.get("lastMessageAt") or "")
            if st.get("pause_scripts") or st.get("human_takeover"):
                return (3, ts)
            if is_no_status(conv):
                return (0, ts)
            if not st.get("last_processed_msg_id"):
                return (1, ts)
            return (2, ts)

        scored: list[tuple[tuple[int, str], dict]] = []
        for c in inbound_convs:
            scored.append((await _priority(c), c))
        scored.sort(key=lambda x: x[0][0])
        inbound_convs = [c for _, c in scored]
        inbound_convs.sort(
            key=lambda c: str(c.get("lastMessageAt") or ""), reverse=True
        )
        inbound = len(inbound_convs)
        skipped = {"paused": 0, "done": 0, "no_script": 0}
        max_plans = max(1, int(os.getenv("PAGER_MAX_REPLIES", "2")))
        pager_user_id = resolve_operator_user_id(
            _settings.pager_user_id,
            account.get("pager_user_id"),
            org_slug=org_slug,
        )
        send_buf = _CycleSendBuffer(
            account,
            org_id=org_id,
            org_slug=org_slug,
            locale=str(account.get("pager_locale") or _settings.pager_locale),
            pager_user_id=pager_user_id,
            client=client,
        )
        planned = 0
        for conv in inbound_convs:
            if planned >= max_plans:
                break
            conv_id = str(conv.get("id") or "")
            try:
                result = await _handle_conversation(
                    bot,
                    account,
                    conv,
                    client,
                    send_buf,
                )
                if result == "paused":
                    skipped["paused"] += 1
                elif result == "done":
                    skipped["done"] += 1
                elif result == "no_script":
                    skipped["no_script"] += 1
                elif result:
                    planned += 1
            except PagerAPIError as exc:
                logger.warning(
                    "conv API error account=%s conv=%s: %s",
                    account.get("id"),
                    conv_id,
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "conv plan failed account=%s conv=%s: %s",
                    account.get("id"),
                    conv_id,
                    exc,
                )

        delivered = 0
        try:
            ok_ids = await send_buf.flush()
            delivered = len(ok_ids)
            if ok_ids:
                logger.info(
                    "Worker account=%s delivered convs=%s",
                    account.get("id"),
                    [c[:8] for c in ok_ids],
                )
        except asyncio.TimeoutError:
            logger.warning(
                "browser batch timeout account=%s",
                account.get("id"),
            )
        except Exception as exc:
            logger.warning(
                "browser batch failed account=%s: %s",
                account.get("id"),
                exc,
            )

        logger.info(
            "Worker account=%s: org=%s channels=%s queue=%s inbound=%s "
            "planned=%s delivered=%s skip=%s",
            account.get("id"),
            (client.org_id or "")[:16],
            len(enabled),
            len(convs),
            inbound,
            planned,
            delivered,
            skipped,
        )

        if client.org_id:
            pager_uid = resolve_operator_user_id(
                _settings.pager_user_id,
                client.session_user_id,
                account.get("pager_user_id"),
                org_slug=str(
                    account.get("org_slug") or _settings.pager_org_slug or ""
                ),
            )
            if not pager_uid and inbound_convs:
                pager_uid = str(
                    inbound_convs[0].get("responsibleuserId")
                    or (inbound_convs[0].get("responsibleUser") or {}).get("id")
                    or ""
                )
            await db.upsert_account(
                int(account["tg_user_id"]),
                org_id=client.org_id,
                org_slug=client.org_slug or account.get("org_slug") or _settings.pager_org_slug,
                pager_user_id=pager_uid,
                session_ok=1,
            )
    except PagerAPIError as exc:
        if is_session_error(exc):
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
            if not accounts:
                logger.warning(
                    "Worker tick: no accounts (need auto_reply=1, paused=0)"
                )
            else:
                logger.info("Worker tick: accounts=%s", len(accounts))
            for acc in accounts:
                try:
                    await asyncio.wait_for(
                        _process_account(bot, acc),
                        timeout=600.0,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Worker account=%s: cycle timeout 600s",
                        acc.get("id"),
                    )
        except Exception:
            logger.exception("worker tick failed")
        await asyncio.sleep(_settings.poll_sec)


def start_worker(bot: Bot) -> asyncio.Task:
    global _worker_task
    if _worker_task and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(worker_loop(bot))
    return _worker_task
