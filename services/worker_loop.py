"""Background worker: poll Pager accounts and run script engine."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from aiogram import Bot

import database as db
from config import (
    SCRIPTS_DIR,
    load_settings,
    resolve_account_operator_id,
    resolve_pager_org_id,
    resolve_operator_user_id,
)
from services.ai_intent import (
    Intent,
    classify,
    is_commitment_reply,
    is_deposit_confirmation,
    is_deposit_question,
    is_deferral_reply,
    is_funnel_positive_reaction,
    is_messenger_reaction_attachment,
    is_ready_for_registration,
    is_registration_complete,
    is_registration_pending,
    needs_human_for_text,
    wants_details_after_intro,
    wants_registration_followup,
    wants_registration_link,
)
from services.encryption import Secrets
from services.image_extract import extract_id_from_image_url, extract_id_from_text
from services.pager_api import (
    PagerAPIError,
    PagerClient,
    is_org_id_error,
    is_session_error,
    message_accepted,
)
from services.pager_browser_send import send_batch_via_browser
from services.session_refresh import refresh_pager_session
from services.script_engine import (
    infer_step_from_history,
    infer_step_from_thread,
    load_script,
    scripts_for_positive_reply,
    resolve_funnel_scripts,
    resolve_eg_backlog_fallback,
    scripts_for_registration_resend,
    scripts_to_resend_for_step,
    script_sent_in_history,
    script_ui_snippet,
    scripts_to_send_after_intent,
    filter_auto_script_keys,
    should_send_deposit_script,
)
from services.status_ids import (
    EXCELLENT,
    ZM_STATUSES,
    infer_step_from_status,
    is_no_status,
    process_funnel_folders,
    should_process_conversation,
)
from services.telegram_notify import notify_escalation

logger = logging.getLogger(__name__)
_settings = load_settings()
_secrets = Secrets(_settings.encryption_key)

_worker_task: asyncio.Task | None = None
# In-process Clerk cookies — DB copy often loses org context between ticks.
_session_cache: dict[int, tuple[float, dict[str, str]]] = {}
_SESSION_CACHE_TTL = 3600.0


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
        batch_chunk_size: int = 6,
        parallel: int = 4,
    ) -> None:
        self.account = account
        self.org_id = org_id
        self.org_slug = org_slug
        self.locale = locale
        self.pager_user_id = pager_user_id
        self.client = client
        self.batch_chunk_size = max(1, batch_chunk_size)
        self.parallel = max(1, parallel)
        self._jobs: dict[str, list[str]] = {}
        self._script_keys: dict[str, list[str]] = {}
        self._clients: dict[str, str] = {}
        self._channels: dict[str, str] = {}
        self._commits: list[tuple[str, dict[str, Any]]] = []
        self._status_patches: dict[str, list[str]] = {}
        self._order: list[str] = []

    def queue_send(
        self,
        conv_id: str,
        texts: list[str],
        *,
        script_keys: list[str] | None = None,
        client_name: str = "",
        channel_id: str = "",
    ) -> None:
        bodies = [t.strip() for t in texts if (t or "").strip()]
        keys = [k.strip() for k in (script_keys or []) if (k or "").strip()]
        keys = filter_auto_script_keys(keys)
        if not bodies and not keys:
            return
        if bodies:
            bucket = self._jobs.setdefault(conv_id, [])
            bucket.extend(bodies)
        if keys:
            sk = self._script_keys.setdefault(conv_id, [])
            sk.extend(keys)
        if client_name:
            self._clients[conv_id] = client_name.strip()
        ch = (channel_id or "").strip()
        if ch:
            self._channels[conv_id] = ch
        if conv_id not in self._order:
            self._order.append(conv_id)

    def queue_script_send(
        self,
        conv_id: str,
        script_keys: list[str],
        *,
        client_name: str = "",
        channel_id: str = "",
    ) -> None:
        self.queue_send(
            conv_id,
            [],
            script_keys=script_keys,
            client_name=client_name,
            channel_id=channel_id,
        )

    def queue_commit(self, conv_id: str, **kwargs: Any) -> None:
        for i, (cid, fields) in enumerate(self._commits):
            if cid == conv_id:
                self._commits[i] = (cid, {**fields, **kwargs})
                return
        self._commits.append((conv_id, dict(kwargs)))

    def queue_status_patch(self, conv_id: str, status_id: str) -> None:
        sid = (status_id or "").strip()
        if sid:
            self._status_patches.setdefault(conv_id, []).append(sid)

    async def _apply_fresh_cookies(
        self, fresh_cookies: dict[str, str], account_id: int
    ) -> None:
        if not fresh_cookies:
            return
        merged = dict(self.client.cookies)
        merged.update(fresh_cookies)
        if self.org_id:
            merged["_pager_org_id"] = self.org_id
        if self.pager_user_id:
            merged["_pager_user_id"] = self.pager_user_id
        self.client.cookies = merged
        _session_cache[account_id] = (time.time(), dict(merged))
        try:
            await db.upsert_account(
                int(self.account["tg_user_id"]),
                session_enc=_secrets.encrypt(json.dumps(merged)),
                session_ok=1,
            )
        except Exception:
            pass

    async def _commit_delivered_async(
        self,
        account_id: int,
        ok_ids: set[str],
        *,
        uid: str,
    ) -> None:
        for conv_id, fields in self._commits:
            if conv_id not in ok_ids:
                continue
            try:
                await self.client.mark_conversation_read(conv_id, user_id=uid)
            except Exception:
                pass
            await db.save_conversation_state(
                account_id, conv_id, send_failures=0, **fields
            )

    async def _fail_undelivered_async(
        self,
        account_id: int,
        chunk_cids: set[str],
        ok_ids: set[str],
    ) -> None:
        for conv_id, _fields in self._commits:
            if conv_id not in chunk_cids or conv_id in ok_ids:
                continue
            st = await db.get_conversation_state(account_id, conv_id)
            fails = int(st.get("send_failures") or 0) + 1
            patch: dict[str, Any] = {"send_failures": fails}
            if fails >= 5:
                patch["pause_scripts"] = 1
            await db.save_conversation_state(account_id, conv_id, **patch)

    def _rest_script_keys_ok(self, keys: list[str], geo: str) -> bool:
        keys = filter_auto_script_keys(list(keys or []))
        if not keys:
            return False
        for key in keys:
            if not (SCRIPTS_DIR / geo / f"{key}.txt").is_file():
                return False
        return True

    async def _flush_rest_scripts(
        self,
        jobs: list[tuple],
        *,
        account_id: int,
        uid: str,
        rest_parallel: int = 12,
    ) -> tuple[set[str], list[tuple]]:
        """Send funnel scripts via REST (local .txt) — faster than Playwright."""
        geo = str(self.account.get("geo") or "zm")
        eligible: list[tuple] = []
        browser_jobs: list[tuple] = []
        for job in jobs:
            cid, texts, _client, _channel_hint, keys, _patches = job
            keys = filter_auto_script_keys(list(keys or []))
            if not texts and keys and self._rest_script_keys_ok(keys, geo):
                eligible.append(job)
            else:
                browser_jobs.append(job)

        if not eligible:
            return set(), jobs

        sem = asyncio.Semaphore(max(1, rest_parallel))
        job_timeout = float(os.getenv("PAGER_REST_JOB_TIMEOUT", "45"))

        async def _one(job: tuple) -> str | None:
            cid, _texts, _client, channel_hint, keys, _patches = job
            keys = filter_auto_script_keys(list(keys or []))

            async def _run() -> str | None:
                try:
                    conv = await self.client.open_conversation(cid)
                    ch = (channel_hint or "").strip()
                    if not ch and conv:
                        ch = str(conv.get("channelId") or "").strip()
                        nested = conv.get("channel")
                        if not ch and isinstance(nested, dict):
                            ch = str(nested.get("id") or "").strip()
                    if not ch:
                        return None
                    if not await self.client.take_conversation(cid, uid):
                        return None
                    for i, key in enumerate(keys):
                        if i:
                            await asyncio.sleep(0.2)
                        body = load_script(geo, key)
                        result = await self.client.send_message_spa(
                            cid,
                            body,
                            user_id=uid,
                            channel_id=ch,
                            conv=conv,
                        )
                        if not message_accepted(result, uid):
                            logger.warning(
                                "REST script not accepted conv=%s key=%s author=%s fb=%s",
                                cid[:8],
                                key,
                                str(result.get("authorId") or "")[:16],
                                str(result.get("facebookMessageId") or "")[:12],
                            )
                            return None
                    await self.client.mark_conversation_read(cid, user_id=uid)
                    logger.info(
                        "REST script ok conv=%s keys=%s",
                        cid[:8],
                        keys,
                    )
                    return cid
                except Exception as exc:
                    logger.warning(
                        "REST script failed conv=%s keys=%s: %s",
                        cid[:8],
                        keys,
                        exc,
                    )
                    return None

            async with sem:
                try:
                    return await asyncio.wait_for(_run(), timeout=job_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "REST script timeout conv=%s keys=%s",
                        cid[:8],
                        keys,
                    )
                    return None

        results = await asyncio.gather(*(_one(job) for job in eligible))
        ok = {cid for cid in results if cid}
        if ok:
            logger.info(
                "REST scripts delivered=%s/%s (browser skipped)",
                len(ok),
                len(eligible),
            )
            await self._commit_delivered_async(account_id, ok, uid=uid)
        retry = [job for job in eligible if job[0] not in ok]
        return ok, browser_jobs + retry

    async def flush(self) -> set[str]:
        conv_ids_set = set(self._jobs) | set(self._script_keys)
        if not conv_ids_set:
            return set()

        conv_ids = [c for c in self._order if c in conv_ids_set]
        for cid in conv_ids_set:
            if cid not in conv_ids:
                conv_ids.append(cid)

        email = str(self.account.get("email") or "").strip()
        pwd_enc = str(self.account.get("password_enc") or "").strip()
        password = _secrets.decrypt(pwd_enc) if pwd_enc else ""
        if not email or not password:
            raise RuntimeError(
                "Account email/password required — reconnect Pager in Telegram bot"
            )

        jobs = [
            (
                cid,
                self._jobs.get(cid, []),
                self._clients.get(cid, ""),
                self._channels.get(cid, ""),
                self._script_keys.get(cid, []),
                self._status_patches.get(cid, []),
            )
            for cid in conv_ids
        ]
        ok_total: set[str] = set()
        account_id = int(self.account["id"])
        uid = self.pager_user_id

        rest_ok, jobs = await self._flush_rest_scripts(
            jobs, account_id=account_id, uid=uid
        )
        ok_total.update(rest_ok)

        if not jobs:
            self._jobs.clear()
            self._script_keys.clear()
            self._clients.clear()
            self._channels.clear()
            self._commits.clear()
            self._status_patches.clear()
            self._order.clear()
            return ok_total

        # One Playwright login per flush — chunking re-authenticates each slice.
        chunk_size = min(len(jobs), max(self.batch_chunk_size, 48))
        n_chunks = (len(jobs) + chunk_size - 1) // chunk_size
        n_keys = sum(len(job[4]) for job in jobs)
        logger.info(
            "browser batch flush jobs=%s chunks=%s parallel=%s texts=%s script_keys=%s",
            len(jobs),
            n_chunks,
            self.parallel,
            sum(len(job[1]) for job in jobs),
            n_keys,
        )

        for start in range(0, len(jobs), chunk_size):
            chunk = jobs[start : start + chunk_size]
            chunk_no = start // chunk_size + 1
            waves = (len(chunk) + self.parallel - 1) // self.parallel
            timeout = min(1200.0, 120.0 + 55.0 * waves)
            try:
                ok, fresh_cookies = await asyncio.wait_for(
                    send_batch_via_browser(
                        chunk,
                        org_id=self.org_id,
                        org_slug=self.org_slug,
                        user_id=self.pager_user_id,
                        locale=self.locale,
                        email=email,
                        password=password,
                        parallel=self.parallel,
                        geo=str(self.account.get("geo") or "zm"),
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "browser batch chunk timeout chunk=%s/%s jobs=%s",
                    chunk_no,
                    n_chunks,
                    len(chunk),
                )
                ok = set()
                fresh_cookies = {}
            except Exception as exc:
                logger.warning(
                    "browser batch chunk failed chunk=%s/%s: %s",
                    chunk_no,
                    n_chunks,
                    exc,
                )
                ok = set()
                fresh_cookies = {}

            await self._apply_fresh_cookies(fresh_cookies, account_id)

            ok_total.update(ok)
            chunk_cids = {job[0] for job in chunk}
            await self._commit_delivered_async(account_id, ok, uid=uid)
            await self._fail_undelivered_async(account_id, chunk_cids, ok)

            logger.info(
                "browser batch chunk=%s/%s delivered=%s/%s",
                chunk_no,
                n_chunks,
                len(ok),
                len(chunk),
            )

        ok = ok_total

        self._jobs.clear()
        self._script_keys.clear()
        self._clients.clear()
        self._channels.clear()
        self._commits.clear()
        self._status_patches.clear()
        self._order.clear()
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


async def _escalate_once(
    bot: Bot,
    esc_chat: int,
    *,
    account_id: int,
    conv_id: str,
    msg_id: str,
    state: dict[str, Any],
    account: dict[str, Any],
    channel_id: str,
    title: str,
    client_name: str,
    channel_name: str,
    folder: str,
    reason: str,
    last_message: str,
    extra: str = "",
    pause: bool = True,
) -> bool:
    """Notify operator once per inbound message; return True if sent."""
    fresh = await db.get_conversation_state(account_id, conv_id)
    if msg_id and msg_id == str(fresh.get("last_escalation_msg_id") or ""):
        logger.info(
            "conv=%s skip duplicate escalation msg=%s",
            conv_id[:8],
            msg_id[:8],
        )
        return False

    patch: dict[str, Any] = {
        "last_processed_msg_id": msg_id,
        "last_escalation_msg_id": msg_id,
    }
    if pause:
        patch["pause_scripts"] = 1
    # Claim before send — parallel worker ticks must not double-notify TG.
    await db.save_conversation_state(account_id, conv_id, **patch)

    await notify_escalation(
        bot,
        esc_chat,
        title=title,
        client_name=client_name,
        channel_name=channel_name,
        folder=folder,
        reason=reason,
        last_message=last_message,
        conv_id=conv_id,
        extra=extra,
        **_escalation_link_kwargs(account, channel_id),
    )
    return True


async def _handle_conversation(
    bot: Bot,
    account: dict[str, Any],
    conv: dict[str, Any],
    client: PagerClient,
    send_buf: _CycleSendBuffer,
) -> bool | str:
    account_id = int(account["id"])
    conv_id = str(conv.get("id") or "")
    geo = str(account.get("geo") or "zm").strip().lower() or "zm"
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

    if not should_process_conversation(conv, geo=geo):
        return False

    state = await db.get_conversation_state(account_id, conv_id)
    if geo == "zm" and not process_funnel_folders():
        st_step = int(state.get("step") or 0)
        if state.get("human_takeover") or st_step >= 4:
            logger.info(
                "conv=%s skip — handed off after registration link",
                conv_id[:8],
            )
            return "done"
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
    acct_cookies = _cookies(account)
    pager_user_id = resolve_account_operator_id(
        account, acct_cookies, org_slug=org_slug
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

    def _has_failed_ghost_after(last_in_ts: str) -> bool:
        for m in msg_only:
            if str(m.get("createdAt") or "") <= last_in_ts:
                continue
            if not _is_outgoing_direction(str(m.get("messageDirection") or "")):
                continue
            if "oldStatusId" in m or "oldResponsibleId" in m:
                continue
            if not (m.get("text") or "").strip():
                continue
            author = str(m.get("authorId") or "").strip()
            if pager_user_id and author and author != pager_user_id:
                continue
            if m.get("isDelivered") or m.get("facebookMessageId"):
                continue
            return True
        return False

    last_in_ts = str(last_in.get("createdAt") or "")
    text = (last_in.get("text") or "").strip()
    attachments = last_in.get("attachments") or []
    has_image = bool(attachments)
    has_ad = bool(last_in.get("adId") or last_in.get("adUrl"))

    hist_step = max(
        infer_step_from_history(msg_only, pager_user_id, geo=geo),
        infer_step_from_thread(msg_only, geo=geo),
    )
    folder_step = infer_step_from_status(conv)
    stored_step = int(state.get("step") or 0)
    effective_step_early = max(hist_step, stored_step, folder_step)

    op_texts_early = [
        (m.get("text") or "")
        for m in msg_only
        if _is_outgoing_direction(str(m.get("messageDirection") or ""))
        and (m.get("text") or "").strip()
        and (m.get("isDelivered") or m.get("facebookMessageId"))
    ]
    deposit_funnel_early = should_send_deposit_script(
        text,
        effective_step_early,
        op_texts_early,
        folder_step=folder_step,
        geo=geo,
    )
    if deposit_funnel_early and (
        state.get("pause_scripts") or state.get("last_escalation_msg_id")
    ):
        await db.save_conversation_state(
            account_id,
            conv_id,
            pause_scripts=0,
            last_escalation_msg_id="",
        )
        state = {
            **state,
            "pause_scripts": 0,
            "last_escalation_msg_id": "",
        }

    if _has_failed_ghost_after(last_in_ts):
        logger.warning(
            "conv=%s skip — undelivered ghost in thread (no resend)",
            conv_id[:8],
        )
        await db.save_conversation_state(
            account_id, conv_id, last_processed_msg_id=msg_id
        )
        return "done"

    needs_reply = not _has_operator_reply_after(last_in_ts)
    is_retry = False
    if msg_id and msg_id == state.get("last_processed_msg_id"):
        if not needs_reply:
            return "done"
        pre_intent = classify(
            text,
            has_image=has_image,
            has_ad=has_ad,
            geo=geo,
            attachments=attachments,
            funnel_step=max(effective_step_early, 1),
        )
        funnel_retry = (
            effective_step_early >= 1
            and effective_step_early < 6
            and (
                pre_intent in (Intent.POSITIVE, Intent.INTERESTED, Intent.READY)
                or is_funnel_positive_reaction(
                    text,
                    attachments,
                    funnel_step=max(effective_step_early, 1),
                )
                or (
                    geo == "eg"
                    and is_no_status(conv)
                    and pre_intent in (Intent.UNKNOWN, Intent.QUESTION)
                )
            )
        )
        if state.get("pause_scripts") and not deposit_funnel_early and not funnel_retry:
            logger.info(
                "conv=%s skip — already handled (paused, no client reply yet)",
                conv_id[:8],
            )
            return "paused"
        if (
            msg_id
            and msg_id == str(state.get("last_escalation_msg_id") or "")
            and not deposit_funnel_early
            and not funnel_retry
        ):
            logger.info(
                "conv=%s skip — already escalated for this message",
                conv_id[:8],
            )
            return "paused"
        if funnel_retry and (
            state.get("pause_scripts")
            or msg_id == str(state.get("last_escalation_msg_id") or "")
        ) and pre_intent != Intent.COMPLAINT:
            logger.info(
                "conv=%s funnel retry — clearing escalation pause",
                conv_id[:8],
            )
            await db.save_conversation_state(
                account_id,
                conv_id,
                pause_scripts=0,
                last_escalation_msg_id="",
            )
            state = {
                **state,
                "pause_scripts": 0,
                "last_escalation_msg_id": "",
            }
        logger.info(
            "conv=%s retry: was marked processed but no reply sent",
            conv_id[:8],
        )
        is_retry = True

    effective_step = effective_step_early
    if needs_reply:
        step = effective_step
    else:
        step = max(stored_step, hist_step)

    no_status = is_no_status(conv)

    intent = classify(
        text,
        has_image=has_image,
        has_ad=has_ad,
        geo=geo,
        attachments=attachments,
        funnel_step=effective_step,
    )
    thread_has_ad = has_ad or any(
        bool(m.get("adId") or m.get("adUrl")) for m in msg_only
    )
    if (
        no_status
        and effective_step < 1
        and needs_reply
        and thread_has_ad
        and re.fullmatch(r"\d{1,4}", text.strip())
    ):
        intent = Intent.INTERESTED

    client_name = ((conv.get("client") or {}).get("name") or "Client").strip()
    channel_name = ((conv.get("channel") or {}).get("name") or channel_id).strip()
    folder = ((conv.get("status") or {}) or {}).get("name") or ""

    esc_chat = _escalation_chat(account)

    is_reaction_only = (
        is_messenger_reaction_attachment(attachments)
        or is_funnel_positive_reaction(
            text, attachments, funnel_step=effective_step
        )
    )
    has_real_image = has_image and not is_messenger_reaction_attachment(attachments)

    deposit_signal = (
        not is_reaction_only
        and not is_deposit_question(text)
        and (
            intent == Intent.DEPOSIT_DONE
            or is_deposit_confirmation(text)
            or (has_real_image and effective_step >= 6)
        )
    )

    post_intro_followup = bool(
        needs_reply
        and effective_step >= 1
        and effective_step < 4
        and not deposit_signal
        and not is_deferral_reply(text)
        and (
            is_ready_for_registration(text)
            or wants_registration_link(text)
            or is_funnel_positive_reaction(
                text, attachments, funnel_step=effective_step
            )
            or wants_details_after_intro(text)
            or intent
            in (Intent.POSITIVE, Intent.INTERESTED, Intent.READY, Intent.QUESTION)
            or (
                intent == Intent.UNKNOWN
                and geo == "eg"
                and re.search(
                    r"استثمر|أريد|اريد|ايو|نجرب|مهتم|تمام|نعم|موضوع|شغل|ازاي|إزاي",
                    text or "",
                    re.I,
                )
            )
        )
    )

    registration_resend = (
        needs_reply
        and effective_step >= 1
        and effective_step < 6
        and is_registration_pending(text)
    )

    reg_confirmed_funnel = needs_reply and should_send_deposit_script(
        text,
        effective_step,
        op_texts_early,
        folder_step=folder_step,
        geo=geo,
    )

    auto_funnel = (
        post_intro_followup or registration_resend or reg_confirmed_funnel
    )

    if is_deferral_reply(text) and needs_reply and effective_step < 6:
        logger.info(
            "conv=%s deferral — waiting, no scripts text=%r",
            conv_id[:8],
            text[:40],
        )
        await db.save_conversation_state(
            account_id, conv_id, last_processed_msg_id=msg_id
        )
        return "done"

    if (
        not auto_funnel
        and needs_reply
        and msg_id
        and msg_id == str(state.get("last_escalation_msg_id") or "")
        and not reg_confirmed_funnel
    ):
        logger.info(
            "conv=%s skip — already escalated for this message",
            conv_id[:8],
        )
        return "paused"

    logger.info(
        "conv=%s intent=%s step=%s hist_step=%s eff_step=%s post_intro=%s reg_resend=%s reg_ok=%s folder=%r retry=%s text=%r",
        conv_id[:8],
        intent.value,
        step,
        hist_step,
        effective_step,
        post_intro_followup,
        registration_resend,
        reg_confirmed_funnel,
        folder[:24] if folder else "",
        is_retry,
        (text or "(photo)")[:40],
    )

    if state.get("human_takeover"):
        logger.info("conv=%s skipped human_takeover", conv_id[:8])
        return "paused"
    if state.get("pause_scripts") and not auto_funnel:
        logger.info("conv=%s skipped pause_scripts", conv_id[:8])
        return "paused"
    if state.get("pause_scripts") and auto_funnel:
        await db.save_conversation_state(account_id, conv_id, pause_scripts=0)

    async def _outbound_send(
        texts: list[str], *, script_keys: list[str] | None = None
    ) -> None:
        send_buf.queue_send(
            conv_id,
            texts,
            script_keys=script_keys,
            client_name=client_name,
            channel_id=channel_id,
        )

    async def send(text: str) -> None:
        send_buf.queue_send(
            conv_id, [text], client_name=client_name, channel_id=channel_id
        )

    # Thumbs-up / FB like after link — not a deposit.
    if (
        is_reaction_only
        and needs_reply
        and effective_step >= 4
        and effective_step < 7
    ):
        logger.info(
            "conv=%s reaction-only at step=%s — skip deposit escalation",
            conv_id[:8],
            effective_step,
        )
        await db.save_conversation_state(
            account_id, conv_id, last_processed_msg_id=msg_id
        )
        return "done"

    # --- Deposit done / deposit screenshot (never resend 04+05) ---
    if deposit_signal and needs_reply and effective_step >= 4:
        op_outgoing = [
            (m.get("text") or "")
            for m in msg_only
            if _valid_outgoing_reply(m)
        ]
        gid = extract_id_from_text(text, geo=geo)
        if not gid and has_real_image:
            img_url = ""
            for att in attachments:
                if att.get("type") == "image":
                    img_url = (att.get("payload") or {}).get("url") or ""
                    break
            if img_url:
                gid = await extract_id_from_image_url(
                    img_url, _settings.openai_api_key, geo=geo
                )

        if has_real_image:
            deposit_reason = "Клиент прислал скрин — проверьте депозит"
        elif is_deposit_confirmation(text):
            deposit_reason = (
                "Клиент написал что сделал депозит (без скрина) — "
                "проверьте вручную или попросите скрин"
            )
        else:
            deposit_reason = "Возможный депозит — проверьте чат"

        await _escalate_once(
            bot,
            esc_chat,
            account_id=account_id,
            conv_id=conv_id,
            msg_id=msg_id,
            state=state,
            account=account,
            channel_id=channel_id,
            title="Депозит",
            client_name=client_name,
            channel_name=channel_name,
            folder=folder,
            reason=deposit_reason,
            last_message=text or "(photo)",
            extra=f"Game ID: {gid}" if gid else "",
            pause=False,
        )

        next_keys: list[str] = []
        # After deposit screenshot — operator checks; TG scripts later.

        if next_keys:
            send_buf.queue_script_send(
                conv_id,
                next_keys,
                client_name=client_name,
                channel_id=channel_id,
            )
            new_step = 6 if "07_game_id" in next_keys else 9
            if gid:
                send_buf.queue_commit(
                    conv_id,
                    step=new_step,
                    extracted_game_id=gid,
                    last_processed_msg_id=msg_id,
                )
            else:
                send_buf.queue_commit(
                    conv_id,
                    step=new_step,
                    last_processed_msg_id=msg_id,
                )
            return True

        await db.save_conversation_state(
            account_id,
            conv_id,
            step=max(step, 7),
            extracted_game_id=gid or state.get("extracted_game_id"),
            last_processed_msg_id=msg_id,
        )
        return True

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
    if (
        needs_human_for_text(intent, step, text, no_status=no_status, geo=geo)
        and not auto_funnel
        and intent != Intent.IMAGE_ONLY
    ):
        escalated = await _escalate_once(
            bot,
            esc_chat,
            account_id=account_id,
            conv_id=conv_id,
            msg_id=msg_id,
            state=state,
            account=account,
            channel_id=channel_id,
            title="Нужен оператор",
            client_name=client_name,
            channel_name=channel_name,
            folder=folder,
            reason=f"Intent: {intent.value}, step {step}",
            last_message=text or "(photo)",
        )
        if escalated and pager_user_id:
            try:
                await client.mark_conversation_read(conv_id, user_id=pager_user_id)
            except Exception:
                pass
        return True if escalated else "paused"

    actions_sent = False

    # --- Image: account ID or deposit screenshot ---
    if has_image:
        img_url = ""
        for att in attachments:
            if att.get("type") == "image":
                img_url = (att.get("payload") or {}).get("url") or ""
                break
        extracted = extract_id_from_text(text, geo=geo)
        if not extracted and img_url:
            extracted = await extract_id_from_image_url(
                img_url, _settings.openai_api_key, geo=geo
            )

        if step >= 5 and step < 7:
            if extracted:
                await _outbound_send([EXCELLENT], script_keys=["06_deposit"])
                if pager_user_id:
                    send_buf.queue_status_patch(
                        conv_id, ZM_STATUSES["registration"]
                    )
                send_buf.queue_commit(
                    conv_id,
                    step=7,
                    extracted_game_id=extracted,
                    last_processed_msg_id=msg_id,
                )
                await _escalate_once(
                    bot,
                    esc_chat,
                    account_id=account_id,
                    conv_id=conv_id,
                    msg_id=msg_id,
                    state=state,
                    account=account,
                    channel_id=channel_id,
                    title="Game ID распознан",
                    client_name=client_name,
                    channel_name=channel_name,
                    folder=folder,
                    reason="Проверьте депозит при необходимости",
                    last_message=text or "(photo)",
                    extra=f"ID: {extracted}",
                    pause=False,
                )
                return True
            await _escalate_once(
                bot,
                esc_chat,
                account_id=account_id,
                conv_id=conv_id,
                msg_id=msg_id,
                state=state,
                account=account,
                channel_id=channel_id,
                title="Фото — ID не распознан",
                client_name=client_name,
                channel_name=channel_name,
                folder=folder,
                reason="Нужен оператор",
                last_message="(photo)",
                pause=True,
            )
            return True

        if step >= 7:
            await _escalate_once(
                bot,
                esc_chat,
                account_id=account_id,
                conv_id=conv_id,
                msg_id=msg_id,
                state=state,
                account=account,
                channel_id=channel_id,
                title="Скрин депозита",
                client_name=client_name,
                channel_name=channel_name,
                folder=folder,
                reason="Подтвердите депозит вручную",
                last_message="(photo)",
                pause=False,
            )
            await _outbound_send(
                [EXCELLENT],
                script_keys=["08_tg_invite", "09_tg_link"]
                if geo != "eg"
                else [],
            )
            if pager_user_id and geo != "eg":
                send_buf.queue_status_patch(
                    conv_id, ZM_STATUSES["deps_pending"]
                )
            send_buf.queue_commit(
                conv_id, step=9, last_processed_msg_id=msg_id
            )
            return True

    # --- Text game ID at wait_id step ---
    if intent == Intent.GAME_ID_TEXT:
        gid = extract_id_from_text(text, geo=geo)
        if gid and step >= 5 and step < 7:
            await _outbound_send([EXCELLENT], script_keys=["06_deposit"])
            if pager_user_id:
                send_buf.queue_status_patch(
                    conv_id, ZM_STATUSES["registration"]
                )
            send_buf.queue_commit(
                conv_id,
                step=7,
                extracted_game_id=gid,
                last_processed_msg_id=msg_id,
            )
            return True

    # --- Script chain (strict funnel order) ---
    op_outgoing = [
        (m.get("text") or "")
        for m in msg_only
        if _valid_outgoing_reply(m)
    ]
    keys: list[str] = []
    if needs_reply and not deposit_signal:
        keys = resolve_funnel_scripts(
            effective_step,
            text,
            intent.value,
            outgoing_texts=op_outgoing,
            attachments=attachments,
            geo=geo,
        )
        if keys:
            logger.info(
                "conv=%s funnel eff_step=%s intent=%s keys=%s folder=%r",
                conv_id[:8],
                effective_step,
                intent.value,
                keys,
                (folder[:20] if folder else ""),
            )
    if not keys and geo == "eg" and is_no_status(conv):
        keys = resolve_eg_backlog_fallback(
            effective_step, op_outgoing, intent.value
        )
        if keys:
            logger.info(
                "conv=%s EG backlog fallback eff_step=%s keys=%s",
                conv_id[:8],
                effective_step,
                keys,
            )

    if needs_reply and not deposit_signal and not keys:
        if (
            intent in (Intent.QUESTION, Intent.UNKNOWN)
            and step >= 4
            and not auto_funnel
            and not (geo == "eg" and is_no_status(conv))
        ):
            escalated = await _escalate_once(
                bot,
                esc_chat,
                account_id=account_id,
                conv_id=conv_id,
                msg_id=msg_id,
                state=state,
                account=account,
                channel_id=channel_id,
                title="Нужен оператор",
                client_name=client_name,
                channel_name=channel_name,
                folder=folder,
                reason=f"Вопрос на шаге {step}: {intent.value}",
                last_message=text or "(photo)",
                pause=True,
            )
            return True if escalated else "paused"
        logger.info(
            "conv=%s no script eff_step=%s intent=%s — wait",
            conv_id[:8],
            effective_step,
            intent.value,
        )
        return "no_script"

    if not needs_reply and not keys:
        if geo == "eg" and is_no_status(conv):
            keys = resolve_eg_backlog_fallback(
                effective_step, op_outgoing, intent.value
            )
        if not keys:
            await db.save_conversation_state(
                account_id, conv_id, last_processed_msg_id=msg_id
            )
            return "done"

    keys = filter_auto_script_keys(keys)

    if intent == Intent.JOINED and effective_step >= 8:
        await db.save_conversation_state(
            account_id, conv_id, step=10, last_processed_msg_id=msg_id
        )
        return True

    if keys:
        logger.info(
            "conv=%s sending %s saved reply(s) keys=%s",
            conv_id[:8],
            len(keys),
            keys,
        )
        send_buf.queue_script_send(
            conv_id,
            keys,
            client_name=client_name,
            channel_id=channel_id,
        )
        actions_sent = True

    new_step = step
    reg_keys = {"04_registration", "05_link"}
    explain_keys = {"02_how_it_works", "03_zmw_table"}
    reg_handoff = geo == "zm" and bool(reg_keys.intersection(keys))
    if reg_handoff:
        new_step = 4
        if pager_user_id:
            send_buf.queue_status_patch(
                conv_id, ZM_STATUSES["in_progress"]
            )
    elif explain_keys.intersection(keys):
        new_step = max(new_step, 2 if geo == "eg" else 3)
    elif keys == ["01_intro"]:
        new_step = max(new_step, 1)
    elif "06_deposit" in keys:
        new_step = max(new_step, 7)
        if pager_user_id:
            send_buf.queue_status_patch(
                conv_id, ZM_STATUSES["registration"]
            )
    elif "07_game_id" in keys:
        new_step = max(new_step, 6)
        if pager_user_id:
            send_buf.queue_status_patch(conv_id, ZM_STATUSES["wait_id"])

    if actions_sent:
        commit: dict[str, Any] = {
            "step": new_step,
            "last_processed_msg_id": msg_id,
        }
        if reg_handoff:
            # After reg+link — operator takes over; bot must not re-enter.
            commit["human_takeover"] = 1
            commit["pause_scripts"] = 0
        send_buf.queue_commit(conv_id, **commit)
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
    """Return user-enabled channel ids only — never auto-enable."""
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
        await db.sync_channels(account_id, api_chs, default_enabled=False)
        logger.info(
            "Worker account=%s: synced %s channel(s) — all off until user enables in 📡 Каналы",
            account_id,
            len(api_chs),
        )
        return set()

    return {c["channel_id"] for c in chs if c.get("enabled")}


async def _process_account(bot: Bot, account: dict[str, Any]) -> None:
    try:
        account_id = int(account["id"])
        cookies = _cookies(account)
        cached = _session_cache.get(account_id)
        if cached and (time.time() - cached[0]) < _SESSION_CACHE_TTL:
            cookies = cached[1]
        if not cookies:
            logger.warning(
                "Worker account=%s: no cookies — refreshing session",
                account_id,
            )
            fresh = await refresh_pager_session(account)
            if not fresh:
                return
            _session_cache[account_id] = (time.time(), dict(fresh))
            acc = await db.get_account_by_tg(int(account["tg_user_id"]))
            if acc:
                account.update(acc)
            cookies = fresh

        org_slug = str(
            account.get("org_slug") or _settings.pager_org_slug or ""
        ).strip()
        org_id = resolve_pager_org_id(
            cookies.get("_pager_org_id"),
            str(account.get("org_id") or ""),
            _settings.pager_org_id,
            org_slug=org_slug,
        )

        def _make_client(cookie_dict: dict[str, str]) -> PagerClient:
            session_uid = resolve_account_operator_id(
                account, cookie_dict, org_slug=org_slug
            )
            merged = dict(cookie_dict)
            cookie_org = str(merged.get("_pager_org_id") or "").strip()
            if org_id and not cookie_org.startswith("org_"):
                merged["_pager_org_id"] = org_id
            if session_uid:
                merged["_pager_user_id"] = session_uid
            return PagerClient(
                _settings.pager_base_url,
                merged,
                org_id=org_id,
                org_slug=org_slug,
                locale=str(account.get("pager_locale") or _settings.pager_locale),
                org_id_fallback=org_id,
                session_user_id=session_uid,
            )

        client = _make_client(cookies)
        await client.warm_session()
        session_ok = False
        for attempt in range(2):
            try:
                await client.list_conversations(page_size=1)
                session_ok = True
                break
            except PagerAPIError as exc:
                if attempt == 0 and is_session_error(exc):
                    logger.info(
                        "Worker account=%s: poll retry after warm",
                        account_id,
                    )
                    if is_org_id_error(exc):
                        await client.resolve_org_id_live()
                    await client.warm_session()
                    continue
                logger.warning(
                    "Worker account=%s: poll failed: %s",
                    account_id,
                    exc,
                )
                break
        if not session_ok:
            logger.warning(
                "Worker account=%s: REST poll failed — refreshing session",
                account_id,
            )
            fresh = await refresh_pager_session(account)
            if not fresh:
                return
            _session_cache[account_id] = (time.time(), dict(fresh))
            acc = await db.get_account_by_tg(int(account["tg_user_id"]))
            if acc:
                account.update(acc)
            client = _make_client(fresh)
            await client.warm_session()
            await client.resolve_org_id_live()
            try:
                await client.list_conversations(page_size=1)
            except PagerAPIError as exc:
                logger.error(
                    "Worker account=%s: session still invalid after refresh: %s",
                    account_id,
                    exc,
                )
                return
        else:
            _session_cache[account_id] = (time.time(), dict(client.cookies))

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

        channel_folders = await db.build_channel_folders_map(account_id, enabled)

        try:
            convs = await client.collect_conversations(
                enabled,
                max_pages=10 if len(enabled) <= 1 else 6,
                geo=str(account.get("geo") or "zm"),
                channel_folders=channel_folders,
            )
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
            channel_folders = await db.build_channel_folders_map(account_id, enabled)
            convs = await client.collect_conversations(
                enabled,
                max_pages=10 if len(enabled) <= 1 else 6,
                geo=str(account.get("geo") or "zm"),
                channel_folders=channel_folders,
            )
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
        funnel_inbound = [c for c in inbound_convs if not is_no_status(c)]
        if no_status_inbound:
            logger.info(
                "Worker account=%s: backlog mode — «Без статусу»=%s + funnel follow-ups=%s",
                account.get("id"),
                len(no_status_inbound),
                len(funnel_inbound),
            )
            seen_ids: set[str] = set()
            merged: list[dict] = []
            for c in no_status_inbound + funnel_inbound:
                cid = str(c.get("id") or "")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    merged.append(c)
            inbound_convs = merged

        def _last_msg_ts(conv: dict) -> float:
            raw = str(conv.get("lastMessageAt") or "").strip()
            if not raw:
                return 0.0
            try:
                from datetime import datetime

                return datetime.fromisoformat(
                    raw.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                return 0.0

        async def _priority(conv: dict) -> tuple[int, float]:
            cid = str(conv.get("id") or "")
            st = await db.get_conversation_state(account_id, cid)
            ts = _last_msg_ts(conv)
            fails = int(st.get("send_failures") or 0)
            st_step = int(st.get("step") or 0)
            status_id = str(conv.get("statusId") or "").strip()
            if is_no_status(conv) and st_step < 1:
                return (-3, ts + fails * 1e12)
            if status_id == ZM_STATUSES["in_progress"] and (
                st.get("pause_scripts") or st.get("last_escalation_msg_id")
            ):
                return (-2, ts)
            if st.get("human_takeover"):
                return (4, ts)
            if st.get("pause_scripts") and st_step < 4:
                return (4, ts)
            if st_step >= 1 and not st.get("pause_scripts"):
                return (-1, ts)
            if is_no_status(conv):
                return (0, ts + fails * 1e12)
            return (1, ts)

        scored: list[tuple[tuple[int, float], dict]] = []
        for c in inbound_convs:
            scored.append((await _priority(c), c))
        scored.sort(key=lambda x: (x[0][0], -x[0][1]))
        inbound_convs = [c for _, c in scored]

        async def _in_funnel(conv: dict) -> bool:
            cid = str(conv.get("id") or "")
            st = await db.get_conversation_state(account_id, cid)
            return int(st.get("step") or 0) >= 1

        funnel_active = [c for c in inbound_convs if await _in_funnel(c)]
        funnel_active_ids = {str(c.get("id") or "") for c in funnel_active}
        fresh_inbound = [
            c for c in inbound_convs if str(c.get("id") or "") not in funnel_active_ids
        ]
        if funnel_active:
            logger.info(
                "Worker account=%s: funnel-active=%s fresh-inbound=%s",
                account.get("id"),
                len(funnel_active),
                len(fresh_inbound),
            )
        # Keep priority sort — do not push all funnel chats ahead of new leads.
        process_order = inbound_convs

        inbound = len(process_order)
        skipped = {"paused": 0, "done": 0, "no_script": 0}
        no_status_n = sum(1 for c in inbound_convs if is_no_status(c))
        n_enabled = len(enabled)
        base_plans = max(1, int(os.getenv("PAGER_MAX_REPLIES", "32")))
        batch_env = (os.getenv("PAGER_BROWSER_BATCH_SIZE") or "").strip()
        try:
            batch_env_n = int(batch_env) if batch_env else 0
        except ValueError:
            batch_env_n = 0
        if batch_env_n >= 4:
            batch_chunk = batch_env_n
        else:
            batch_chunk = 16 if n_enabled <= 1 else 8
        par_env = (os.getenv("PAGER_BROWSER_PARALLEL") or "").strip()
        try:
            par_env_n = int(par_env) if par_env else 0
        except ValueError:
            par_env_n = 0
        browser_parallel = par_env_n if par_env_n >= 1 else 3
        if n_enabled <= 1:
            max_plans = min(24, base_plans)
        elif no_status_n > 15:
            max_plans = min(28, base_plans)
            batch_chunk = max(batch_chunk, 12)
            browser_parallel = min(max(browser_parallel, 4), 6)
        elif no_status_n > 8:
            max_plans = min(12, base_plans)
        else:
            max_plans = min(8, base_plans)
        pager_user_id = resolve_account_operator_id(
            account, cookies, org_slug=org_slug
        )
        funnel_cap = max(8, (max_plans * 2 + 1) // 3)
        max_handle = max(32, int(os.getenv("PAGER_MAX_HANDLE", "72") or "72"))
        logger.info(
            "Worker account=%s: plan budget=%s funnel_cap=%s batch=%s parallel=%s "
            "max_handle=%s (no_status=%s)",
            account.get("id"),
            max_plans,
            funnel_cap,
            batch_chunk,
            browser_parallel,
            max_handle,
            no_status_n,
        )
        send_buf = _CycleSendBuffer(
            account,
            org_id=org_id,
            org_slug=org_slug,
            locale=str(account.get("pager_locale") or _settings.pager_locale),
            pager_user_id=pager_user_id,
            client=client,
            batch_chunk_size=batch_chunk,
            parallel=browser_parallel,
        )
        planned = 0
        funnel_planned = 0
        max_total = max_plans + funnel_cap
        handled = 0
        for conv in process_order:
            if handled >= max_handle or planned + funnel_planned >= max_total:
                break
            handled += 1
            cid = str(conv.get("id") or "")
            in_funnel = cid in funnel_active_ids
            if in_funnel and funnel_planned >= funnel_cap:
                continue
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
                    if in_funnel:
                        funnel_planned += 1
                    else:
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
            pager_uid = resolve_account_operator_id(
                account, client.cookies, org_slug=org_slug
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
                        timeout=1200.0,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Worker account=%s: cycle timeout 1200s",
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
