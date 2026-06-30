"""Background worker: poll Pager accounts and run script engine."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
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
    is_deposit_acknowledgment,
    is_deposit_confirmation,
    is_affirmative_to_deposit_check,
    client_replied_to_ready_broadcast,
    client_replied_to_operator_broadcast,
    is_deposit_question,
    deposit_screenshot_nudge_reply,
    is_already_registered_before_funnel,
    is_age_answer,
    is_deferral_reply,
    is_deposit_tier_choice,
    is_funnel_positive_reaction,
    is_positive_message_reaction,
    is_reaction_only_message,
    is_refusal_reply,
    is_messenger_reaction_attachment,
    is_post_link_registration_question,
    is_ready_for_registration,
    is_registration_confirmed,
    is_registration_pending,
    is_short_affirmative,
    is_xbet_site_question,
    money_refusal_reply,
    phone_chat_only_reply,
    xbet_site_confirm_reply,
    needs_human_for_text,
    wants_details_after_intro,
    wants_registration_followup,
    wants_registration_link,
)
from services.encryption import Secrets
from services.llm_client import (
    llm_router_enabled,
    llm_router_may_send,
    llm_router_mode,
    llm_router_strict,
    resolve_llm_api_key,
)
from services.llm_learn import (
    format_learn_feedback,
    learn_account_allowed,
    learn_notify_enabled,
    learn_scan_completed_chats,
    record_live_learn_success,
)
from services.llm_router import route_funnel_message
from services.image_extract import (
    classify_screenshot_kind,
    extract_id_from_image_url,
    extract_id_from_text,
    looks_like_game_id,
)
from services.pager_api import (
    PagerAPIError,
    PagerClient,
    is_org_id_error,
    is_session_error,
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
    resolve_zm_backlog_fallback,
    funnel_step_from_script_gaps,
    reg_link_sent_in_history,
    reg_link_script_key,
    reg_bundle_pending_link,
    reg_script_keys_set,
    deposit_script_key,
    game_id_script_key,
    link_help_script_keys,
    scripts_for_registration_resend,
    scripts_to_resend_for_step,
    script_sent_in_history,
    script_ui_snippet,
    scripts_to_send_after_intent,
    filter_auto_script_keys,
    bodies_for_script_keys,
    browser_first_geos,
    extract_game_id,
    should_send_deposit_script,
)
from services.status_ids import (
    EXCELLENT,
    ZM_STATUSES,
    ALL_INBOX_FOLDER_ID,
    NO_STATUS_FOLDER_ID,
    conv_allowed_in_folders,
    conv_folder_key,
    funnel_status_ids,
    infer_step_from_status,
    is_no_status,
    normalize_enabled_folders,
    resolve_funnel_statuses,
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


def resolve_conv_geo(account: dict[str, Any], channel_id: str) -> str:
    """Geo for a chat: per-channel setting, else account default."""
    default = db.normalize_channel_geo(str(account.get("geo") or "zm"))
    cmap = account.get("_channel_geo") or {}
    cid = (channel_id or "").strip()
    if cid and cid in cmap:
        return db.normalize_channel_geo(str(cmap[cid]), default=default)
    return default


def _stored_game_id(state: dict[str, Any]) -> str:
    return str(state.get("extracted_game_id") or "").strip()


def _is_waiting_for_game_id(
    step: int,
    conv_status_id: str,
    funnel_statuses: dict[str, str],
) -> bool:
    wait_id_sid = str(funnel_statuses.get("wait_id") or "").strip()
    return step >= 6 or bool(wait_id_sid and conv_status_id == wait_id_sid)


def _game_id_from_client(
    text: str,
    msg_only: list[dict[str, Any]],
    *,
    geo: str,
) -> str:
    """Parse game ID from this message or a recent client line («I have sent»)."""
    for raw in ((text or "").strip(),):
        for candidate in (
            extract_id_from_text(raw, geo=geo),
            extract_game_id(raw, geo=geo),
        ):
            if candidate and looks_like_game_id(candidate, geo=geo):
                return candidate
    if re.search(
        r"i have sent|already sent|i sent|je l'ai envoy|envoy[eé]|"
        r"ارسلت|أرسلت|بعت|ابعت",
        (text or ""),
        re.I,
    ):
        for m in reversed(msg_only):
            if not _is_incoming_direction(str(m.get("messageDirection") or "")):
                continue
            body = (m.get("text") or "").strip()
            if not body:
                continue
            for candidate in (
                extract_id_from_text(body, geo=geo),
                extract_game_id(body, geo=geo),
            ):
                if candidate and looks_like_game_id(candidate, geo=geo):
                    return candidate
    return ""


def _is_deposit_screenshot_without_gid(
    *,
    has_real_image: bool,
    extracted: str,
    stored_gid: str,
    geo: str,
) -> bool:
    """Balance/profile screenshot without a recognizable game ID."""
    if not has_real_image:
        return False
    if stored_gid:
        return False
    if extracted and looks_like_game_id(extracted, geo=geo):
        return False
    return True


def _round_robin_by_channel(convs: list[dict]) -> list[dict]:
    """Fair merge — EG/CM backlog must not starve DJ (or any) channel."""
    if len(convs) <= 1:
        return convs
    by_ch: dict[str, list[dict]] = defaultdict(list)
    order: list[str] = []
    for conv in convs:
        ch = str(conv.get("channelId") or "")
        if ch not in by_ch:
            order.append(ch)
        by_ch[ch].append(conv)
    if len(order) <= 1:
        return convs
    out: list[dict] = []
    idx = {ch: 0 for ch in order}
    while True:
        progressed = False
        for ch in order:
            i = idx[ch]
            if i < len(by_ch[ch]):
                out.append(by_ch[ch][i])
                idx[ch] = i + 1
                progressed = True
        if not progressed:
            break
    return out


def _interleave_process_order(
    scored: list[tuple[tuple[int, float], dict]],
    *,
    limit: int,
) -> list[dict]:
    """Each cycle: advance fresh replies (Oui after intro) and backlog together."""
    fresh: list[dict] = []
    backlog: list[dict] = []
    other: list[dict] = []
    for (prio, _), conv in scored:
        if prio <= -5:
            fresh.append(conv)
        elif prio <= -2:
            backlog.append(conv)
        else:
            other.append(conv)
    fresh = _round_robin_by_channel(fresh)
    backlog = _round_robin_by_channel(backlog)
    other = _round_robin_by_channel(other)
    out: list[dict] = []
    fi = bi = oi = 0
    if len(backlog) > 400:
        fresh_burst = 4
    elif len(backlog) > 100:
        fresh_burst = 3
    else:
        fresh_burst = 2
    while len(out) < limit:
        progressed = False
        for _ in range(fresh_burst):
            if fi < len(fresh) and len(out) < limit:
                out.append(fresh[fi])
                fi += 1
                progressed = True
        if bi < len(backlog) and len(out) < limit:
            out.append(backlog[bi])
            bi += 1
            progressed = True
        if oi < len(other) and len(out) < limit:
            out.append(other[oi])
            oi += 1
            progressed = True
        if not progressed:
            break
    return out


def _build_process_order(
    scored: list[tuple[tuple[int, float], dict]],
    *,
    limit: int,
    no_status_n: int,
    queue_n: int,
) -> list[dict]:
    """Prefer fresh «Без статусу» when the queue is huge — funnel backlog can wait."""
    if (
        _env_truthy("PAGER_NO_STATUS_FIRST", default=True)
        and no_status_n > 0
        and queue_n > 150
    ):
        hot: list[dict] = []
        for (prio, _), conv in scored:
            if prio <= -4 or is_no_status(conv):
                hot.append(conv)
        if hot:
            hot = _round_robin_by_channel(hot)
            if len(hot) >= min(limit, max(24, no_status_n // 2)):
                return hot[:limit]
    return _interleave_process_order(scored, limit=limit)


@dataclass
class _AccountCycleCtx:
    """Per-cycle caches — avoid thousands of SQLite round-trips per account."""

    account_id: int
    enabled: set[str]
    allowed_folders: set[str] | None
    state_map: dict[str, dict[str, Any]]

    def conv_state(self, conv_id: str) -> dict[str, Any]:
        st = self.state_map.get(conv_id)
        if st:
            return st
        return db.default_conversation_state(self.account_id, conv_id)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _flush_browser_first() -> bool:
    return _env_truthy("PAGER_FLUSH_BROWSER_FIRST", default=False)


def _compute_cycle_limits(
    queue_n: int,
    no_status_n: int,
    n_enabled: int,
) -> dict[str, int]:
    """Scale throughput with queue size — large traffic must not wait days."""
    base_plans = max(1, int(os.getenv("PAGER_MAX_REPLIES", "128")))
    max_handle = max(128, int(os.getenv("PAGER_MAX_HANDLE", "400") or "400"))
    try:
        batch_chunk = int(os.getenv("PAGER_BROWSER_BATCH_SIZE") or "0")
    except ValueError:
        batch_chunk = 0
    try:
        browser_parallel = int(os.getenv("PAGER_BROWSER_PARALLEL") or "0")
    except ValueError:
        browser_parallel = 0
    try:
        plan_parallel = int(os.getenv("PAGER_PLAN_PARALLEL") or "0")
    except ValueError:
        plan_parallel = 0

    if batch_chunk < 4:
        batch_chunk = 32 if n_enabled <= 1 else 24
    if browser_parallel < 1:
        browser_parallel = 10 if n_enabled <= 1 else 8
    if plan_parallel < 1:
        plan_parallel = 32 if n_enabled <= 1 else 48

    if queue_n > 50:
        max_handle = min(max_handle, 160)
        base_plans = min(max(base_plans, 64), 96)
        batch_chunk = max(min(batch_chunk, 20), 16)
        browser_parallel = max(browser_parallel, 6)
        plan_parallel = max(plan_parallel, 40)

    if queue_n > 300:
        max_handle = min(max_handle, 120)
        base_plans = min(base_plans, 96)
        batch_chunk = min(max(batch_chunk, 12), 16)
        browser_parallel = min(max(browser_parallel, 6), 8)
        plan_parallel = min(max(plan_parallel, 48), 64)

    max_plans = min(base_plans, max_handle)
    if no_status_n > 15 and n_enabled > 1:
        max_plans = min(max(max_plans, no_status_n // 3), 112)

    funnel_cap = max(max_plans, 64)
    return {
        "max_handle": max_handle,
        "max_plans": max_plans,
        "funnel_cap": funnel_cap,
        "batch_chunk": batch_chunk,
        "browser_parallel": browser_parallel,
        "plan_parallel": plan_parallel,
    }


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
        self._lock = threading.Lock()
        self._jobs: dict[str, list[str]] = {}
        self._script_keys: dict[str, list[str]] = {}
        self._clients: dict[str, str] = {}
        self._channels: dict[str, str] = {}
        self._geos: dict[str, str] = {}
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
        geo: str = "",
    ) -> None:
        with self._lock:
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
            g = (geo or "").strip().lower()
            if g:
                self._geos[conv_id] = db.normalize_channel_geo(g)
            if conv_id not in self._order:
                self._order.append(conv_id)

    def queue_script_send(
        self,
        conv_id: str,
        script_keys: list[str],
        *,
        client_name: str = "",
        channel_id: str = "",
        geo: str = "",
    ) -> None:
        self.queue_send(
            conv_id,
            [],
            script_keys=script_keys,
            client_name=client_name,
            channel_id=channel_id,
            geo=geo,
        )

    def queue_commit(self, conv_id: str, **kwargs: Any) -> None:
        with self._lock:
            for i, (cid, fields) in enumerate(self._commits):
                if cid == conv_id:
                    self._commits[i] = (cid, {**fields, **kwargs})
                    return
            self._commits.append((conv_id, dict(kwargs)))

    def queue_status_patch(self, conv_id: str, status_id: str) -> None:
        with self._lock:
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
        self._pin_org_on_client()
        _session_cache[account_id] = (time.time(), dict(merged))
        try:
            await db.upsert_account(
                int(self.account["tg_user_id"]),
                session_enc=_secrets.encrypt(json.dumps(merged)),
                session_ok=1,
            )
        except Exception:
            pass

    def _pin_org_on_client(self) -> None:
        """Keep org id on client after browser batches — avoids «Organization ID required»."""
        if not self.org_id:
            return
        self.client.org_id = self.org_id
        self.client.org_id_fallback = self.org_id
        self.client.cookies["_pager_org_id"] = self.org_id

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

    def _partition_send_jobs(
        self, jobs: list[tuple]
    ) -> tuple[list[tuple], list[tuple]]:
        """Split outbound jobs into browser vs REST-eligible queues."""
        account_geo = db.normalize_channel_geo(str(self.account.get("geo") or "zm"))
        browser_jobs: list[tuple] = []
        rest_jobs: list[tuple] = []
        for job in jobs:
            cid, texts, _client, _channel_hint, keys, _patches = job[:6]
            job_geo = (
                str(job[6]).strip().lower()
                if len(job) > 6 and job[6]
                else self._geos.get(cid) or account_geo
            )
            job_geo = db.normalize_channel_geo(job_geo, default=account_geo)
            keys = filter_auto_script_keys(list(keys or []))
            if job_geo in browser_first_geos():
                browser_jobs.append((*job[:6], job_geo))
                continue
            if not texts and keys and self._rest_script_keys_ok(keys, job_geo):
                rest_jobs.append((*job[:6], job_geo))
            else:
                browser_jobs.append((*job[:6], job_geo))
        return browser_jobs, rest_jobs

    def _order_jobs_for_flush(self, jobs: list[tuple]) -> list[tuple]:
        """REST-eligible jobs first so a cycle cap still delivers most replies quickly."""
        browser_jobs, rest_jobs = self._partition_send_jobs(jobs)
        try:
            max_browser = int(os.getenv("PAGER_MAX_BROWSER_JOBS", "12") or "12")
        except ValueError:
            max_browser = 12
        if max_browser > 0 and len(browser_jobs) > max_browser:
            logger.info(
                "browser job cap account=%s jobs=%s -> %s (rest=%s)",
                self.account.get("id"),
                len(browser_jobs),
                max_browser,
                len(rest_jobs),
            )
            browser_jobs = browser_jobs[:max_browser]
        return rest_jobs + browser_jobs

    async def _flush_browser_capped(
        self,
        jobs: list[tuple],
        *,
        account_id: int,
        uid: str,
        email: str,
        password: str,
    ) -> set[str]:
        if not jobs:
            return set()
        try:
            timeout = float(os.getenv("PAGER_BROWSER_CYCLE_TIMEOUT", "240") or "240")
        except ValueError:
            timeout = 240.0
        try:
            return await asyncio.wait_for(
                self._flush_browser_batches(
                    jobs,
                    account_id=account_id,
                    uid=uid,
                    email=email,
                    password=password,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "browser cycle timeout account=%s jobs=%s limit=%ss",
                account_id,
                len(jobs),
                int(timeout),
            )
            return set()

    async def _flush_rest_scripts(
        self,
        jobs: list[tuple],
        *,
        account_id: int,
        uid: str,
        rest_parallel: int = 64,
    ) -> tuple[set[str], list[tuple]]:
        """Send funnel scripts via REST (local .txt) — faster than Playwright."""
        account_geo = db.normalize_channel_geo(str(self.account.get("geo") or "zm"))
        eligible: list[tuple] = []
        browser_jobs: list[tuple] = []
        for job in jobs:
            cid, texts, _client, _channel_hint, keys, _patches = job[:6]
            job_geo = (
                str(job[6]).strip().lower()
                if len(job) > 6 and job[6]
                else self._geos.get(cid) or account_geo
            )
            job_geo = db.normalize_channel_geo(job_geo, default=account_geo)
            keys = filter_auto_script_keys(list(keys or []))
            if job_geo in browser_first_geos():
                logger.debug(
                    "REST skip browser-first geo=%s conv=%s keys=%s",
                    job_geo,
                    cid[:8],
                    keys,
                )
                browser_jobs.append((*job[:6], job_geo))
                continue
            if not texts and keys and self._rest_script_keys_ok(keys, job_geo):
                eligible.append((*job[:6], job_geo))
            else:
                browser_jobs.append((*job[:6], job_geo))

        if not eligible:
            return set(), jobs

        rest_parallel = max(1, int(os.getenv("PAGER_REST_PARALLEL", "20") or "20"))
        script_gap = float(os.getenv("PAGER_REST_SCRIPT_GAP", "0.25") or "0.25")
        reg_gap = float(os.getenv("PAGER_REST_REG_GAP", "0.9") or "0.9")
        sem = asyncio.Semaphore(rest_parallel)
        job_timeout = float(os.getenv("PAGER_REST_JOB_TIMEOUT", "90"))

        async def _one(job: tuple) -> str | None:
            cid, _texts, _client, channel_hint, keys, patches, geo = job
            keys = filter_auto_script_keys(list(keys or []))
            status_patches = list(patches or [])

            async def _run() -> str | None:
                try:
                    conv: dict[str, Any] = {}
                    try:
                        uid_send, conv = await self.client.prepare_outbound(
                            cid, author_id=uid
                        )
                    except PagerAPIError as exc:
                        logger.warning(
                            "REST prepare outbound conv=%s: %s",
                            cid[:8],
                            exc.body[:120],
                        )
                        return None
                    ch = (channel_hint or "").strip()
                    if not ch:
                        ch = str(conv.get("channelId") or "").strip()
                        nested = conv.get("channel")
                        if not ch and isinstance(nested, dict):
                            ch = str(nested.get("id") or "").strip()
                    if not ch:
                        return None
                    bodies = bodies_for_script_keys(geo, keys)
                    for i, body in enumerate(bodies):
                        if i:
                            gap = reg_gap if len(bodies) > 1 else script_gap
                            if gap > 0:
                                await asyncio.sleep(gap)
                        if not await self.client.send_body_reliable(
                            cid,
                            body,
                            user_id=uid_send,
                            channel_id=ch,
                            conv=conv,
                        ):
                            logger.warning(
                                "REST ghost conv=%s keys=%s body=%r",
                                cid[:8],
                                keys,
                                body[:48],
                            )
                            return None
                        logger.info(
                            "REST body ok conv=%s chars=%s",
                            cid[:8],
                            len(body),
                        )
                    if status_patches:
                        sid = status_patches[-1]
                        try:
                            await self.client.patch_status(
                                cid, sid, user_id=uid_send
                            )
                            logger.info(
                                "REST status patch conv=%s status=%s",
                                cid[:8],
                                sid[:8],
                            )
                        except Exception as exc:
                            logger.warning(
                                "REST status patch failed conv=%s: %s",
                                cid[:8],
                                exc,
                            )
                    await self.client.mark_conversation_read(
                        cid, user_id=uid_send
                    )
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

    async def _flush_browser_batches(
        self,
        jobs: list[tuple],
        *,
        account_id: int,
        uid: str,
        email: str,
        password: str,
    ) -> set[str]:
        if not jobs:
            return set()
        chunk_size = min(len(jobs), max(4, self.batch_chunk_size))
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
        ok_total: set[str] = set()
        for start in range(0, len(jobs), chunk_size):
            chunk = jobs[start : start + chunk_size]
            chunk_no = start // chunk_size + 1
            waves = (len(chunk) + self.parallel - 1) // self.parallel
            timeout = min(2400.0, 180.0 + 90.0 * waves)
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
                        geo=db.normalize_channel_geo(
                            str(self.account.get("geo") or "zm")
                        ),
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
            finally:
                self._pin_org_on_client()

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
        return ok_total

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
                self._geos.get(cid)
                or resolve_conv_geo(self.account, self._channels.get(cid, "")),
            )
            for cid in conv_ids
        ]
        jobs = self._order_jobs_for_flush(jobs)
        try:
            max_send = int(os.getenv("PAGER_MAX_SEND_JOBS", "120") or "120")
        except ValueError:
            max_send = 120
        if max_send > 0 and len(jobs) > max_send:
            logger.info(
                "send cap account=%s jobs=%s -> %s (next cycle continues)",
                self.account.get("id"),
                len(jobs),
                max_send,
            )
            jobs = jobs[:max_send]
        ok_total: set[str] = set()
        account_id = int(self.account["id"])
        uid = self.pager_user_id

        if _flush_browser_first():
            browser_jobs, rest_jobs = self._partition_send_jobs(jobs)
            logger.info(
                "send flush browser-first browser=%s rest=%s",
                len(browser_jobs),
                len(rest_jobs),
            )
            ok_total.update(
                await self._flush_browser_capped(
                    browser_jobs,
                    account_id=account_id,
                    uid=uid,
                    email=email,
                    password=password,
                )
            )
            rest_ok, leftover = await self._flush_rest_scripts(
                rest_jobs, account_id=account_id, uid=uid
            )
            ok_total.update(rest_ok)
            if leftover:
                ok_total.update(
                    await self._flush_browser_capped(
                        leftover,
                        account_id=account_id,
                        uid=uid,
                        email=email,
                        password=password,
                    )
                )
        else:
            rest_ok, browser_jobs = await self._flush_rest_scripts(
                jobs, account_id=account_id, uid=uid
            )
            ok_total.update(rest_ok)
            if browser_jobs:
                logger.info(
                    "send flush REST-first browser_fallback=%s",
                    len(browser_jobs),
                )
                ok_total.update(
                    await self._flush_browser_capped(
                        browser_jobs,
                        account_id=account_id,
                        uid=uid,
                        email=email,
                        password=password,
                    )
                )

        self._jobs.clear()
        self._script_keys.clear()
        self._clients.clear()
        self._channels.clear()
        self._geos.clear()
        self._commits.clear()
        self._status_patches.clear()
        self._order.clear()
        return ok_total


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


def _thread_outgoing_texts(msg_only: list[dict[str, Any]]) -> list[str]:
    """All operator/bot text in thread — includes manual broadcasts (no author filter)."""
    out: list[str] = []
    for m in msg_only:
        if not _is_outgoing_direction(str(m.get("messageDirection") or "")):
            continue
        if "oldStatusId" in m or "oldResponsibleId" in m:
            continue
        t = (m.get("text") or "").strip()
        if t:
            out.append(t)
    return out


def _recent_incoming_messages(
    msg_only: list[dict[str, Any]], *, limit: int = 10
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in reversed(msg_only):
        if not _is_incoming_direction(str(m.get("messageDirection") or "")):
            continue
        if "oldStatusId" in m:
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def _pick_client_turn_message(
    msg_only: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    geo: str,
    hint_step: int,
    op_texts: list[str],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Don't lose tier choice (300) when client then sends sticker or «Chef»."""
    incoming = _recent_incoming_messages(msg_only)
    if not incoming:
        return fallback
    last_processed = str(state.get("last_processed_msg_id") or "")
    if (
        hint_step >= 2
        and hint_step < 6
        and not reg_link_sent_in_history(op_texts, geo=geo)
    ):
        for m in incoming:
            t = (m.get("text") or "").strip()
            if is_deposit_tier_choice(t, geo=geo):
                return m
    for m in incoming:
        mid = str(m.get("id") or "")
        if mid and mid == last_processed:
            continue
        t = (m.get("text") or "").strip()
        atts = m.get("attachments") or []
        if t or atts or is_positive_message_reaction(m.get("reaction")):
            if t or (atts and not is_messenger_reaction_attachment(atts)):
                return m
            if is_messenger_reaction_attachment(atts):
                return m
            if is_positive_message_reaction(m.get("reaction")):
                return m
    return fallback


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
    allow_repeat: bool = False,
) -> bool:
    """Notify operator once per inbound message; return True if sent."""
    fresh = await db.get_conversation_state(account_id, conv_id)
    esc_msg = str(fresh.get("last_escalation_msg_id") or "")
    if esc_msg and not allow_repeat:
        if not msg_id or msg_id == esc_msg:
            logger.info(
                "conv=%s skip duplicate escalation msg=%s",
                conv_id[:8],
                (msg_id or "")[:8],
            )
            return False
        logger.info(
            "conv=%s skip repeat escalation (prior msg=%s)",
            conv_id[:8],
            esc_msg[:8],
        )
        return False
    if (
        msg_id
        and fresh.get("pause_scripts")
        and msg_id == str(fresh.get("last_processed_msg_id") or "")
        and esc_msg
    ):
        logger.info(
            "conv=%s skip duplicate escalation (paused) msg=%s",
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
    cycle_ctx: _AccountCycleCtx,
) -> bool | str:
    account_id = int(account["id"])
    conv_id = str(conv.get("id") or "")
    channel_id = str(conv.get("channelId") or "")
    geo = resolve_conv_geo(account, channel_id)
    funnel_statuses = account.get("_funnel_statuses") or ZM_STATUSES
    active_funnel = funnel_status_ids(funnel_statuses)
    if not conv_id:
        return False

    state = cycle_ctx.conv_state(conv_id)
    allowed_folders = cycle_ctx.allowed_folders

    if not _is_incoming_direction(str(conv.get("lastMessageDirection") or "")):
        if is_no_status(conv):
            if int(state.get("step") or 0) < 1:
                return False
        else:
            status_id_early = str(conv.get("statusId") or "").strip()
            in_funnel = status_id_early in active_funnel
            in_picker = allowed_folders is not None and conv_allowed_in_folders(
                conv, allowed_folders
            )
            if not in_funnel and not in_picker:
                return False

    enabled = cycle_ctx.enabled
    if not enabled or channel_id not in enabled:
        return False

    if not should_process_conversation(
        conv,
        geo=geo,
        funnel_statuses=funnel_statuses,
        allowed_folders=allowed_folders,
    ):
        return False

    if allowed_folders is not None and not conv_allowed_in_folders(
        conv, allowed_folders
    ):
        specific, all_inbox = normalize_enabled_folders(allowed_folders)
        logger.info(
            "conv=%s skip — folder not enabled (status=%s effective=%s all=%s)",
            conv_id[:8],
            conv_folder_key(conv)[:8] or "no_status",
            sorted(specific)[:4],
            all_inbox,
        )
        return False

    completed_sid = str(funnel_statuses.get("completed") or "").strip()
    conv_status_id = str(conv.get("statusId") or "").strip()
    if (
        completed_sid
        and conv_status_id == completed_sid
        and int(state.get("step") or 0) >= 9
    ):
        return "done"
    if int(state.get("send_failures") or 0) >= 5:
        logger.info(
            "conv=%s skipped send_failures=%s (use /reset_pauses)",
            conv_id[:8],
            state.get("send_failures"),
        )
        return "paused"

    status_id = conv_status_id
    incoming = _is_incoming_direction(str(conv.get("lastMessageDirection") or ""))
    if (
        state.get("pause_scripts")
        and state.get("last_processed_msg_id")
        and not incoming
        and status_id not in funnel_status_ids(funnel_statuses)
    ):
        return "paused"
    if state.get("human_takeover") and status_id not in active_funnel:
        if int(state.get("step") or 0) < 4:
            return "paused"

    messages = await client.list_messages(conv_id, page_size=80)
    # API returns newest first — work chronologically
    msg_only = [
        m
        for m in messages
        if m.get("text") is not None or m.get("attachments") or m.get("reaction")
    ]
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

    def _recent_client_image_in_thread(limit: int = 6) -> bool:
        n = 0
        for m in reversed(msg_only):
            if not _is_incoming_direction(str(m.get("messageDirection") or "")):
                continue
            atts = m.get("attachments") or []
            if atts and not is_messenger_reaction_attachment(atts):
                if any(a.get("type") == "image" for a in atts):
                    return True
            n += 1
            if n >= limit:
                break
        return False

    hist_step = max(
        infer_step_from_history(msg_only, pager_user_id, geo=geo),
        infer_step_from_thread(msg_only, geo=geo),
    )
    op_texts_early = [
        (m.get("text") or "")
        for m in msg_only
        if _is_outgoing_direction(str(m.get("messageDirection") or ""))
        and (m.get("text") or "").strip()
        and (m.get("isDelivered") or m.get("facebookMessageId"))
    ]
    thread_out_early = _thread_outgoing_texts(msg_only)
    folder_step = infer_step_from_status(conv, funnel_statuses)
    if folder_step > hist_step and not reg_link_sent_in_history(
        op_texts_early, geo=geo
    ):
        folder_step = hist_step
    stored_step = int(state.get("step") or 0)
    if is_no_status(conv) and not reg_link_sent_in_history(
        op_texts_early, geo=geo
    ):
        effective_step_early = funnel_step_from_script_gaps(
            thread_out_early,
            geo=geo,
            stored_step=stored_step,
        )
    else:
        effective_step_early = max(hist_step, stored_step, folder_step)

    last_in = _pick_client_turn_message(
        msg_only,
        state,
        geo=geo,
        hint_step=effective_step_early,
        op_texts=op_texts_early,
        fallback=last_in,
    )
    turn_text = (last_in.get("text") or "").strip()
    if turn_text != (text or "").strip() or str(last_in.get("id") or "") != msg_id:
        logger.info(
            "conv=%s client turn text=%r (was %r)",
            conv_id[:8],
            turn_text[:40] or "(attach)",
            (text or "")[:40] or "(attach)",
        )
    msg_id = str(last_in.get("id") or "")
    last_in_ts = str(last_in.get("createdAt") or "")
    text = (last_in.get("text") or "").strip()
    attachments = last_in.get("attachments") or []
    message_reaction = last_in.get("reaction")
    has_image = bool(attachments)
    has_ad = bool(last_in.get("adId") or last_in.get("adUrl"))
    deposit_funnel_early = should_send_deposit_script(
        text,
        effective_step_early,
        op_texts_early,
        folder_step=folder_step,
        geo=geo,
    )
    if deposit_funnel_early and state.get("pause_scripts"):
        await db.save_conversation_state(
            account_id,
            conv_id,
            pause_scripts=0,
        )
        state = {
            **state,
            "pause_scripts": 0,
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
    ready_broadcast_reply = (
        needs_reply
        and geo in ("cm", "dj")
        and reg_link_sent_in_history(op_texts_early, geo=geo)
        and client_replied_to_ready_broadcast(
            msg_only, last_in, text, geo=geo
        )
    )
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
            message_reaction=message_reaction,
        )
        funnel_retry = needs_reply and effective_step_early < 8 and (
            pre_intent
            in (
                Intent.POSITIVE,
                Intent.INTERESTED,
                Intent.READY,
                Intent.MONEY_REQUEST,
            )
            or is_funnel_positive_reaction(
                text,
                attachments,
                funnel_step=max(effective_step_early, 1),
                geo=geo,
                message_reaction=message_reaction,
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
        if funnel_retry and state.get("pause_scripts") and pre_intent != Intent.COMPLAINT:
            logger.info(
                "conv=%s funnel retry — unpausing scripts (keep escalation dedupe)",
                conv_id[:8],
            )
            await db.save_conversation_state(
                account_id,
                conv_id,
                pause_scripts=0,
            )
            state = {
                **state,
                "pause_scripts": 0,
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
        message_reaction=message_reaction,
    )

    if needs_reply and intent == Intent.DECLINED:
        logger.info(
            "conv=%s declined — pause, no scripts text=%r",
            conv_id[:8],
            (text or "")[:40],
        )
        await db.save_conversation_state(
            account_id,
            conv_id,
            pause_scripts=1,
            last_processed_msg_id=msg_id,
        )
        if pager_user_id:
            try:
                await client.mark_conversation_read(conv_id, user_id=pager_user_id)
            except Exception:
                pass
        return "done"

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

    if (
        needs_reply
        and is_registration_confirmed(text)
        and reg_link_sent_in_history(op_texts_early, geo=geo)
        and not ready_broadcast_reply
    ):
        reg_done_keys: list[str] = []
        if should_send_deposit_script(
            text,
            effective_step,
            op_texts_early,
            folder_step=folder_step,
            geo=geo,
        ):
            reg_done_keys = ["06_deposit"]
        if reg_done_keys and pager_user_id:
            send_buf.queue_status_patch(conv_id, funnel_statuses["wait_id"])
        elif (
            pager_user_id
            and is_already_registered_before_funnel(text)
            and not reg_done_keys
        ):
            completed_sid = str(funnel_statuses.get("completed") or "").strip()
            if completed_sid:
                send_buf.queue_status_patch(conv_id, completed_sid)
        if reg_done_keys:
            send_buf.queue_script_send(
                conv_id,
                reg_done_keys,
                client_name=client_name,
                channel_id=channel_id,
                geo=geo,
            )
            send_buf.queue_commit(
                conv_id,
                step=max(step, 7),
                last_processed_msg_id=msg_id,
                pause_scripts=0,
            )
        else:
            await db.save_conversation_state(
                account_id,
                conv_id,
                step=max(stored_step, 6),
                pause_scripts=1,
                last_processed_msg_id=msg_id,
            )
        logger.info(
            "conv=%s reg confirmed — wait_id=%s keys=%s",
            conv_id[:8],
            (funnel_statuses.get("wait_id") or "")[:8],
            reg_done_keys,
        )
        return True

    if (
        needs_reply
        and geo in ("cm", "dj", "zm")
        and is_deposit_tier_choice(text, geo=geo)
        and not reg_link_sent_in_history(op_texts_early, geo=geo)
    ):
        tier_sn = script_ui_snippet(
            "04_tier" if geo == "cm" else "03_zmw_table", geo
        )
        tier_seen = script_sent_in_history(
            op_texts_early, tier_sn
        ) or script_sent_in_history(thread_out_early, tier_sn)
        if tier_seen or geo == "cm":
            reg_keys = (
                ["05_registration", "06_link", "07_chrome"]
                if geo == "cm"
                else ["04_registration", "05_link"]
            )
            send_buf.queue_script_send(
                conv_id,
                reg_keys,
                client_name=client_name,
                channel_id=channel_id,
                geo=geo,
            )
            send_buf.queue_commit(
                conv_id,
                step=max(step, 5),
                last_processed_msg_id=msg_id,
                pause_scripts=0,
            )
            logger.info(
                "conv=%s tier-choice reg keys=%s text=%r",
                conv_id[:8],
                reg_keys,
                (text or "")[:20],
            )
            return True

    if (
        needs_reply
        and wants_registration_link(text)
        and not reg_link_sent_in_history(op_texts_early, geo=geo)
        and geo in ("cm", "zm", "dj")
    ):
        reg_keys = (
            ["05_registration", "06_link", "07_chrome"]
            if geo == "cm"
            else ["04_registration", "05_link"]
        )
        send_buf.queue_script_send(
            conv_id,
            reg_keys,
            client_name=client_name,
            channel_id=channel_id,
            geo=geo,
        )
        send_buf.queue_commit(
            conv_id,
            step=max(step, 5),
            last_processed_msg_id=msg_id,
            pause_scripts=0,
        )
        logger.info(
            "conv=%s link-request reg keys=%s text=%r",
            conv_id[:8],
            reg_keys,
            (text or "")[:40],
        )
        return True

    pending_link = reg_bundle_pending_link(
        list(dict.fromkeys(thread_out_early + op_texts_early)),
        geo=geo,
    )
    client_prompted_link_retry = (
        is_short_affirmative(text)
        or wants_registration_link(text)
        or is_deposit_tier_choice(text, geo=geo)
    )
    if (
        needs_reply
        and pending_link
        and client_prompted_link_retry
        and geo in ("cm", "eg", "zm", "dj")
    ):
        link_key = reg_link_script_key(geo)
        send_buf.queue_script_send(
            conv_id,
            [link_key],
            client_name=client_name,
            channel_id=channel_id,
            geo=geo,
        )
        send_buf.queue_commit(
            conv_id,
            step=max(step, 5),
            last_processed_msg_id=msg_id,
            pause_scripts=0,
        )
        logger.info(
            "conv=%s reg link retry key=%s text=%r",
            conv_id[:8],
            link_key,
            (text or "")[:30],
        )
        return True

    if needs_reply and ready_broadcast_reply:
        nudge_sn = script_ui_snippet("extras/deposit_screenshot_nudge", geo)
        if not script_sent_in_history(thread_out_early, nudge_sn) and not (
            script_sent_in_history(op_texts_early, nudge_sn)
        ):
            body = deposit_screenshot_nudge_reply(geo=geo)
            send_buf.queue_send(
                conv_id,
                [body],
                client_name=client_name,
                channel_id=channel_id,
                geo=geo,
            )
            send_buf.queue_commit(
                conv_id,
                step=max(step, 5),
                last_processed_msg_id=msg_id,
                pause_scripts=0,
            )
            logger.info(
                "conv=%s ready-broadcast nudge folder=%r text=%r",
                conv_id[:8],
                (folder[:20] if folder else ""),
                (text or "")[:40],
            )
            return True

    broadcast_funnel_reply = (
        needs_reply
        and geo == "eg"
        and client_replied_to_operator_broadcast(
            msg_only,
            last_in,
            text,
            geo=geo,
            message_reaction=message_reaction,
        )
    )
    if broadcast_funnel_reply:
        op_out_bc = [
            (m.get("text") or "")
            for m in msg_only
            if _valid_outgoing_reply(m)
        ]
        keys = resolve_eg_backlog_fallback(
            effective_step, op_out_bc, "positive"
        )
        if not keys:
            keys = resolve_funnel_scripts(
                effective_step,
                text,
                Intent.POSITIVE.value,
                outgoing_texts=op_out_bc,
                attachments=attachments,
                geo=geo,
                message_reaction=message_reaction,
            )
        keys = [k for k in keys if k != game_id_script_key(geo)]
        if keys:
            send_buf.queue_script_send(
                conv_id,
                keys,
                client_name=client_name,
                channel_id=channel_id,
                geo=geo,
            )
            send_buf.queue_commit(
                conv_id,
                step=max(step, effective_step),
                last_processed_msg_id=msg_id,
                pause_scripts=0,
            )
            logger.info(
                "conv=%s EG broadcast-like reply keys=%s",
                conv_id[:8],
                keys,
            )
            return True

    if (
        needs_reply
        and is_deposit_question(text)
        and 4 <= effective_step < 9
        and reg_link_sent_in_history(op_texts_early, geo=geo)
    ):
        table_sn = script_ui_snippet("03_zmw_table", geo)
        dep_sn = script_ui_snippet("06_deposit", geo)
        if script_sent_in_history(op_texts_early, dep_sn):
            dq_keys = ["03_zmw_table"]
        elif script_sent_in_history(op_texts_early, table_sn):
            dq_keys = ["03_zmw_table"]
        else:
            dq_keys = ["06_deposit"]
        send_buf.queue_script_send(
            conv_id,
            dq_keys,
            client_name=client_name,
            channel_id=channel_id,
            geo=geo,
        )
        new_dq_step = 7 if "06_deposit" in dq_keys else max(step, 3)
        send_buf.queue_commit(
            conv_id,
            step=new_dq_step,
            last_processed_msg_id=msg_id,
            pause_scripts=0,
        )
        if pager_user_id and (
            "06_deposit" in dq_keys
            or (completed_sid and conv_status_id == completed_sid)
        ):
            send_buf.queue_status_patch(conv_id, funnel_statuses["wait_id"])
        logger.info(
            "conv=%s deposit question keys=%s text=%r",
            conv_id[:8],
            dq_keys,
            (text or "")[:50],
        )
        return True

    is_reaction_only = is_reaction_only_message(
        text,
        attachments,
        message_reaction=message_reaction,
    )
    has_real_image = has_image and not is_messenger_reaction_attachment(attachments)

    dep_sn_early = script_ui_snippet(deposit_script_key(geo), geo)
    dep_script_sent = script_sent_in_history(op_texts_early, dep_sn_early)
    reg_link_sent = reg_link_sent_in_history(op_texts_early, geo=geo)

    if (
        needs_reply
        and has_real_image
        and not (text or "").strip()
        and reg_link_sent
        and not dep_script_sent
    ):
        img_url = ""
        for att in attachments:
            if att.get("type") == "image":
                img_url = (att.get("payload") or {}).get("url") or ""
                break
        shot_kind = "other"
        if img_url and resolve_llm_api_key():
            shot_kind = await classify_screenshot_kind(
                img_url,
                resolve_llm_api_key(),
                cookies=client.cookies,
            )
        if shot_kind in ("link_error", "registration", "other"):
            help_keys = link_help_script_keys(geo)
            send_buf.queue_script_send(
                conv_id,
                help_keys,
                client_name=client_name,
                channel_id=channel_id,
                geo=geo,
            )
            send_buf.queue_commit(
                conv_id,
                step=max(step, effective_step),
                last_processed_msg_id=msg_id,
                pause_scripts=0,
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
                title="Ссылка не открывается",
                client_name=client_name,
                channel_name=channel_name,
                folder=folder,
                reason="Скрин похож на ошибку ссылки — отправлен Chrome + линк",
                last_message="(photo)",
                extra=f"vision={shot_kind}",
                pause=False,
            )
            logger.info(
                "conv=%s post-link screenshot kind=%s keys=%s",
                conv_id[:8],
                shot_kind,
                help_keys,
            )
            return True

    deposit_signal = (
        not is_reaction_only
        and not is_deposit_question(text)
        and (
            intent == Intent.DEPOSIT_DONE
            or is_deposit_confirmation(text)
            or is_deposit_acknowledgment(text)
            or is_affirmative_to_deposit_check(
                text, op_texts_early, geo=geo
            )
            or (
                has_real_image
                and reg_link_sent
                and (
                    dep_script_sent
                    or script_sent_in_history(
                        op_texts_early,
                        script_ui_snippet(game_id_script_key(geo), geo),
                    )
                )
            )
        )
    )

    post_intro_max = 6 if geo == "cm" else 4
    post_intro_followup = bool(
        needs_reply
        and effective_step >= 1
        and effective_step < post_intro_max
        and not deposit_signal
        and not is_deferral_reply(text)
        and not is_refusal_reply(text)
        and (
            is_ready_for_registration(text, geo=geo)
            or wants_registration_link(text)
            or is_funnel_positive_reaction(
                text,
                attachments,
                funnel_step=effective_step,
                geo=geo,
                message_reaction=message_reaction,
            )
            or wants_details_after_intro(text)
            or is_post_link_registration_question(text)
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
        and not is_registration_confirmed(text)
        and not reg_link_sent_in_history(op_texts_early, geo=geo)
    )

    reg_confirmed_funnel = needs_reply and should_send_deposit_script(
        text,
        effective_step,
        op_texts_early,
        folder_step=folder_step,
        geo=geo,
    )
    ready_broadcast_reply = (
        needs_reply
        and geo in ("cm", "dj")
        and reg_link_sent_in_history(op_texts_early, geo=geo)
        and client_replied_to_ready_broadcast(
            msg_only, last_in, text, geo=geo
        )
    )
    xbet_site_reply = needs_reply and is_xbet_site_question(text) and (
        reg_link_sent_in_history(op_texts_early, geo=geo)
        or "?" in (text or "")
    )

    auto_funnel = (
        post_intro_followup
        or registration_resend
        or reg_confirmed_funnel
        or ready_broadcast_reply
        or xbet_site_reply
    )
    script_funnel = (
        needs_reply
        and effective_step < 8
        and intent
        in (
            Intent.POSITIVE,
            Intent.INTERESTED,
            Intent.READY,
            Intent.MONEY_REQUEST,
        )
    )
    funnel_active = auto_funnel or script_funnel

    if needs_reply and intent == Intent.PHONE_REQUEST:
        body = phone_chat_only_reply(text, geo=geo)
        send_buf.queue_send(
            conv_id,
            [body],
            client_name=client_name,
            channel_id=channel_id,
            geo=geo,
        )
        send_buf.queue_commit(
            conv_id,
            step=max(step, 1),
            last_processed_msg_id=msg_id,
            pause_scripts=0,
        )
        logger.info(
            "conv=%s phone_request reply step=%s text=%r",
            conv_id[:8],
            effective_step,
            (text or "")[:40],
        )
        return True

    if needs_reply and xbet_site_reply:
        body = xbet_site_confirm_reply(geo=geo)
        send_buf.queue_send(
            conv_id,
            [body],
            client_name=client_name,
            channel_id=channel_id,
            geo=geo,
        )
        send_buf.queue_commit(
            conv_id,
            step=max(step, 3),
            last_processed_msg_id=msg_id,
            pause_scripts=0,
        )
        logger.info(
            "conv=%s xbet_site_confirm geo=%s text=%r",
            conv_id[:8],
            geo,
            (text or "")[:50],
        )
        return True

    if needs_reply and intent == Intent.MONEY_REQUEST:
        if effective_step < 6:
            logger.info(
                "conv=%s ignore money_request at funnel step=%s text=%r",
                conv_id[:8],
                effective_step,
                (text or "")[:40],
            )
        else:
            body = money_refusal_reply(text, geo=geo)
            send_buf.queue_send(
                conv_id,
                [body],
                client_name=client_name,
                channel_id=channel_id,
                geo=geo,
            )
            send_buf.queue_commit(
                conv_id,
                step=max(step, 1),
                last_processed_msg_id=msg_id,
                pause_scripts=0,
            )
            return True

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
        not funnel_active
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
        "conv=%s ch=%s geo=%s intent=%s step=%s hist_step=%s eff_step=%s post_intro=%s reg_resend=%s reg_ok=%s folder=%r retry=%s text=%r",
        conv_id[:8],
        channel_id[:8] if channel_id else "?",
        geo,
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
        sid = str(conv.get("statusId") or "").strip()
        st_step = int(state.get("step") or 0)
        if sid not in active_funnel and st_step < 4:
            logger.info("conv=%s skipped human_takeover", conv_id[:8])
            return "paused"
    if state.get("pause_scripts") and not funnel_active:
        if is_no_status(conv) and needs_reply:
            await db.save_conversation_state(
                account_id,
                conv_id,
                pause_scripts=0,
                send_failures=0,
            )
            state = {**state, "pause_scripts": 0, "send_failures": 0}
            logger.info(
                "conv=%s no_status unpause — resume backlog",
                conv_id[:8],
            )
        else:
            logger.info("conv=%s skipped pause_scripts", conv_id[:8])
            return "paused"
    if state.get("pause_scripts") and funnel_active:
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
            geo=geo,
        )

    async def send(text: str) -> None:
        send_buf.queue_send(
            conv_id, [text], client_name=client_name, channel_id=channel_id
        )

    # Thumbs-up / FB like after link — send deposit script if link was already sent.
    if (
        is_reaction_only
        and needs_reply
        and effective_step >= 4
        and effective_step < 7
        and reg_link_sent_in_history(op_texts_early, geo=geo)
    ):
        link_sn = script_ui_snippet("05_link", geo)
        dep_sn = script_ui_snippet(deposit_script_key(geo), geo)
        if script_sent_in_history(
            op_texts_early, link_sn
        ) and not script_sent_in_history(op_texts_early, dep_sn):
            send_buf.queue_script_send(
                conv_id,
                [deposit_script_key(geo)],
                client_name=client_name,
                channel_id=channel_id,
            )
            send_buf.queue_commit(
                conv_id,
                step=max(step, 7),
                last_processed_msg_id=msg_id,
            )
            if pager_user_id:
                send_buf.queue_status_patch(
                    conv_id, funnel_statuses["wait_id"]
                )
            return True
        logger.info(
            "conv=%s reaction-only at step=%s — no deposit script",
            conv_id[:8],
            effective_step,
        )
        if geo == "eg" and effective_step < 8:
            op_out_rx = [
                (m.get("text") or "")
                for m in msg_only
                if _valid_outgoing_reply(m)
            ]
            rx_keys = resolve_eg_backlog_fallback(
                effective_step, op_out_rx, "positive"
            )
            if not rx_keys:
                rx_keys = resolve_funnel_scripts(
                    effective_step,
                    text,
                    Intent.POSITIVE.value,
                    outgoing_texts=op_out_rx,
                    attachments=attachments,
                    geo=geo,
                    message_reaction=message_reaction,
                )
            rx_keys = [k for k in rx_keys if k != game_id_script_key(geo)]
            if rx_keys:
                send_buf.queue_script_send(
                    conv_id,
                    rx_keys,
                    client_name=client_name,
                    channel_id=channel_id,
                    geo=geo,
                )
                send_buf.queue_commit(
                    conv_id,
                    step=max(step, effective_step),
                    last_processed_msg_id=msg_id,
                    pause_scripts=0,
                )
                logger.info(
                    "conv=%s reaction-only funnel keys=%s",
                    conv_id[:8],
                    rx_keys,
                )
                return True
        return "no_script"

    client_gid = _game_id_from_client(text, msg_only, geo=geo)
    if (
        needs_reply
        and client_gid
        and _is_waiting_for_game_id(step, conv_status_id, funnel_statuses)
    ):
        stored_gid = _stored_game_id(state)
        if stored_gid == client_gid and step >= 7:
            await db.save_conversation_state(
                account_id, conv_id, last_processed_msg_id=msg_id
            )
            return "done"
        op_gid_out = [
            (m.get("text") or "")
            for m in msg_only
            if _valid_outgoing_reply(m)
        ]
        tg_sn = script_ui_snippet("09_tg_link", geo)
        if script_sent_in_history(op_gid_out, tg_sn):
            send_buf.queue_commit(
                conv_id,
                step=max(step, 8),
                extracted_game_id=client_gid,
                last_processed_msg_id=msg_id,
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
            title="Game ID распознан",
            client_name=client_name,
            channel_name=channel_name,
            folder=folder,
            reason="Проверьте депозит при необходимости",
            last_message=text or client_gid,
            extra=f"ID: {client_gid}",
            pause=False,
        )
        send_buf.queue_script_send(
            conv_id,
            ["08_tg_invite", "09_tg_link"],
            client_name=client_name,
            channel_id=channel_id,
            geo=geo,
        )
        if pager_user_id:
            send_buf.queue_status_patch(
                conv_id, funnel_statuses.get("deps_pending") or funnel_statuses["wait_id"]
            )
        send_buf.queue_commit(
            conv_id,
            step=max(step, 8),
            extracted_game_id=client_gid,
            last_processed_msg_id=msg_id,
            pause_scripts=0,
        )
        logger.info(
            "conv=%s game_id accepted gid=%s step=%s",
            conv_id[:8],
            client_gid,
            step,
        )
        if llm_router_mode() == "learn":
            await record_live_learn_success(
                account_id,
                conv_id,
                message_id=msg_id,
                geo=geo,
                game_id=client_gid,
                screenshot_kind="game_id",
                client_name=client_name,
                folder=folder,
            )
        return True

    # --- Deposit done / deposit screenshot (never resend 04+05) ---
    if deposit_signal and needs_reply and effective_step >= 4:
        op_outgoing = [
            (m.get("text") or "")
            for m in msg_only
            if _valid_outgoing_reply(m)
        ]
        gid_key = game_id_script_key(geo)
        gid_sn = script_ui_snippet(gid_key, geo)
        gid = extract_id_from_text(text, geo=geo)
        img_url = ""
        if has_real_image:
            for att in attachments:
                if att.get("type") == "image":
                    img_url = (att.get("payload") or {}).get("url") or ""
                    break
            if not gid and img_url and resolve_llm_api_key():
                maybe_gid = await extract_id_from_image_url(
                    img_url,
                    resolve_llm_api_key(),
                    geo=geo,
                    cookies=client.cookies,
                )
                if looks_like_game_id(maybe_gid, geo=geo):
                    gid = maybe_gid

        payment_proof = (
            has_real_image
            or _recent_client_image_in_thread()
            or is_deposit_confirmation(text)
            or is_deposit_acknowledgment(text)
            or intent == Intent.DEPOSIT_DONE
            or is_affirmative_to_deposit_check(text, op_texts_early, geo=geo)
        )

        if gid and looks_like_game_id(gid, geo=geo):
            await _outbound_send([EXCELLENT], script_keys=[deposit_script_key(geo)])
            if pager_user_id:
                send_buf.queue_status_patch(conv_id, funnel_statuses["wait_id"])
            send_buf.queue_commit(
                conv_id,
                step=7,
                extracted_game_id=gid,
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
                reason="Депозит + ID — проверьте вручную",
                last_message=text or "(photo)",
                extra=f"ID: {gid}",
                pause=False,
            )
            return True

        if payment_proof:
            stored_gid = _stored_game_id(state)
            deposit_screenshot = _is_deposit_screenshot_without_gid(
                has_real_image=has_real_image,
                extracted=gid,
                stored_gid=stored_gid,
                geo=geo,
            )
            if dep_script_sent and (
                not script_sent_in_history(op_outgoing, gid_sn) or deposit_screenshot
            ):
                send_buf.queue_script_send(
                    conv_id,
                    [gid_key],
                    client_name=client_name,
                    channel_id=channel_id,
                    geo=geo,
                )
                if pager_user_id:
                    send_buf.queue_status_patch(
                        conv_id, funnel_statuses["wait_id"]
                    )
                send_buf.queue_commit(
                    conv_id,
                    step=max(step, 6),
                    last_processed_msg_id=msg_id,
                    pause_scripts=0,
                )
                reason = (
                    "Клиент прислал скрин оплаты — проверьте депозит"
                    if has_real_image or _recent_client_image_in_thread()
                    else "Клиент подтвердил депозит — запрошен ID из кабинета"
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
                    title="Депозит",
                    client_name=client_name,
                    channel_name=channel_name,
                    folder=folder,
                    reason=reason,
                    last_message=text or "(photo)",
                    pause=False,
                )
                logger.info(
                    "conv=%s deposit proof — request game_id keys=%s",
                    conv_id[:8],
                    [gid_key],
                )
                return True

            await db.save_conversation_state(
                account_id,
                conv_id,
                step=max(step, 6),
                last_processed_msg_id=msg_id,
            )
            return True

        if has_real_image:
            deposit_reason = "Клиент прислал скрин — проверьте депозит"
        elif is_deposit_confirmation(text) or is_deposit_acknowledgment(text):
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
    if has_image and not is_reaction_only_message(
        text, attachments, message_reaction=message_reaction
    ):
        img_url = ""
        for att in attachments:
            if att.get("type") == "image":
                img_url = (att.get("payload") or {}).get("url") or ""
                break
        extracted = extract_id_from_text(text, geo=geo)
        if not extracted and img_url and resolve_llm_api_key():
            extracted = await extract_id_from_image_url(
                img_url,
                resolve_llm_api_key(),
                geo=geo,
                cookies=client.cookies,
            )

        wait_id_sid = str(funnel_statuses.get("wait_id") or "").strip()
        waiting_for_game_id = step >= 6 or (
            wait_id_sid and conv_status_id == wait_id_sid
        )

        if waiting_for_game_id and step < 7:
            if extracted:
                await _outbound_send([EXCELLENT], script_keys=["06_deposit"])
                if pager_user_id:
                    send_buf.queue_status_patch(
                        conv_id, funnel_statuses["wait_id"]
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

        if step >= 5 and step < 7 and extracted:
            await _outbound_send([EXCELLENT], script_keys=["06_deposit"])
            if pager_user_id:
                send_buf.queue_status_patch(
                    conv_id, funnel_statuses["wait_id"]
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

        op_out_img = [
            (m.get("text") or "")
            for m in msg_only
            if _valid_outgoing_reply(m)
        ]
        if (
            4 <= step < 7
            and reg_link_sent_in_history(op_out_img, geo=geo)
            and dep_script_sent
            and _is_deposit_screenshot_without_gid(
                has_real_image=has_real_image,
                extracted=extracted,
                stored_gid=_stored_game_id(state),
                geo=geo,
            )
        ):
            gid_key = game_id_script_key(geo)
            send_buf.queue_script_send(
                conv_id,
                [gid_key],
                client_name=client_name,
                channel_id=channel_id,
                geo=geo,
            )
            if pager_user_id:
                send_buf.queue_status_patch(conv_id, funnel_statuses["wait_id"])
            send_buf.queue_commit(
                conv_id,
                step=max(step, 6),
                last_processed_msg_id=msg_id,
                pause_scripts=0,
            )
            logger.info(
                "conv=%s deposit screenshot step=%s — request game_id keys=%s",
                conv_id[:8],
                step,
                [gid_key],
            )
            return True

        # In «В процесі» / registration — random photos are not game-ID screenshots.
        if (
            step < 6
            and has_image
            and not extracted
            and not reg_link_sent_in_history(
                [
                    (m.get("text") or "")
                    for m in msg_only
                    if _valid_outgoing_reply(m)
                ],
                geo=geo,
            )
        ):
            has_image = False

        if step >= 7:
            gid_key = game_id_script_key(geo)
            gid_sn = script_ui_snippet(gid_key, geo)
            stored_gid = _stored_game_id(state)
            if extracted and looks_like_game_id(extracted, geo=geo):
                await _outbound_send([EXCELLENT], script_keys=[deposit_script_key(geo)])
                if pager_user_id:
                    send_buf.queue_status_patch(
                        conv_id, funnel_statuses["wait_id"]
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
                    reason="ID из кабинета — проверьте депозит",
                    last_message=text or "(photo)",
                    extra=f"ID: {extracted}",
                    pause=False,
                )
                return True
            if not script_sent_in_history(
                [
                    (m.get("text") or "")
                    for m in msg_only
                    if _valid_outgoing_reply(m)
                ],
                gid_sn,
            ):
                send_buf.queue_script_send(
                    conv_id,
                    [gid_key],
                    client_name=client_name,
                    channel_id=channel_id,
                    geo=geo,
                )
                if pager_user_id:
                    send_buf.queue_status_patch(
                        conv_id, funnel_statuses["wait_id"]
                    )
                send_buf.queue_commit(
                    conv_id,
                    step=max(step, 6),
                    last_processed_msg_id=msg_id,
                )
                return True
            if _is_deposit_screenshot_without_gid(
                has_real_image=has_real_image,
                extracted=extracted,
                stored_gid=stored_gid,
                geo=geo,
            ):
                send_buf.queue_script_send(
                    conv_id,
                    [gid_key],
                    client_name=client_name,
                    channel_id=channel_id,
                    geo=geo,
                )
                if pager_user_id:
                    send_buf.queue_status_patch(
                        conv_id, funnel_statuses["wait_id"]
                    )
                send_buf.queue_commit(
                    conv_id,
                    step=max(step, 6),
                    last_processed_msg_id=msg_id,
                )
                logger.info(
                    "conv=%s deposit screenshot — re-request game_id keys=%s",
                    conv_id[:8],
                    [gid_key],
                )
                return True
            tg_sn = script_ui_snippet("09_tg_link", geo)
            if not script_sent_in_history(
                [
                    (m.get("text") or "")
                    for m in msg_only
                    if _valid_outgoing_reply(m)
                ],
                tg_sn,
            ):
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
                    script_keys=["08_tg_invite", "09_tg_link"],
                )
                if pager_user_id:
                    send_buf.queue_status_patch(
                        conv_id, funnel_statuses["deps_pending"]
                    )
                send_buf.queue_commit(
                    conv_id, step=9, last_processed_msg_id=msg_id
                )
            return True

    # --- Text game ID (legacy path — early accept handles wait_id) ---
    if intent == Intent.GAME_ID_TEXT:
        gid = _game_id_from_client(text, msg_only, geo=geo)
        if (
            gid
            and step >= 5
            and step < 9
            and not _is_waiting_for_game_id(step, conv_status_id, funnel_statuses)
        ):
            send_buf.queue_commit(
                conv_id,
                step=max(step, 7),
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
            message_reaction=message_reaction,
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
        elif (
            geo == "cm"
            and effective_step < 6
            and intent in (Intent.POSITIVE, Intent.READY, Intent.INTERESTED)
            and (
                is_funnel_positive_reaction(
                    text,
                    attachments,
                    funnel_step=max(effective_step, 1),
                    geo=geo,
                    message_reaction=message_reaction,
                )
                or is_short_affirmative(text)
                or is_age_answer(text)
                or is_reaction_only_message(
                    text, attachments, message_reaction=message_reaction
                )
            )
        ):
            intro_sn = script_ui_snippet("01_intro", geo)
            intro2_sn = script_ui_snippet("01_intro_2", geo)
            age_sn = script_ui_snippet("02_age", geo)
            steps_sn = script_ui_snippet("03_steps", geo)
            tier_sn = script_ui_snippet("04_tier", geo)
            dep_key = deposit_script_key(geo)
            if not script_sent_in_history(op_outgoing, intro_sn):
                keys = ["01_intro", "01_intro_2"]
            elif not script_sent_in_history(op_outgoing, intro2_sn):
                keys = ["01_intro_2"]
            elif not script_sent_in_history(op_outgoing, age_sn):
                keys = ["02_age"]
            elif not script_sent_in_history(op_outgoing, steps_sn):
                if is_age_answer(text) or is_funnel_positive_reaction(
                    text,
                    attachments,
                    funnel_step=max(effective_step, 2),
                    geo=geo,
                    message_reaction=message_reaction,
                ) or is_short_affirmative(text):
                    keys = ["03_steps"]
            elif not script_sent_in_history(op_outgoing, tier_sn):
                keys = ["04_tier"]
            elif is_deposit_tier_choice(text, geo=geo) and not reg_link_sent_in_history(
                op_outgoing, geo=geo
            ):
                keys = ["05_registration", "06_link", "07_chrome"]
            elif (
                script_sent_in_history(op_outgoing, tier_sn)
                and not reg_link_sent_in_history(op_outgoing, geo=geo)
            ):
                keys = ["05_registration", "06_link", "07_chrome"]
            if keys:
                logger.info(
                    "conv=%s CM funnel fallback keys=%s",
                    conv_id[:8],
                    keys,
                )
        elif (
            geo in ("zm", "dj")
            and effective_step < 4
            and intent in (Intent.POSITIVE, Intent.READY, Intent.INTERESTED)
            and (
                is_funnel_positive_reaction(
                    text,
                    attachments,
                    funnel_step=max(effective_step, 1),
                    geo=geo,
                    message_reaction=message_reaction,
                )
                or is_short_affirmative(text)
            )
        ):
            intro_sn = script_ui_snippet("01_intro", geo)
            how_sn = script_ui_snippet("02_how_it_works", geo)
            if script_sent_in_history(op_outgoing, intro_sn) and not script_sent_in_history(
                op_outgoing, how_sn
            ):
                keys = ["02_how_it_works", "03_zmw_table"]
                logger.info(
                    "conv=%s positive-after-intro fallback keys=%s",
                    conv_id[:8],
                    keys,
                )
        elif (
            geo in ("zm", "dj", "cm")
            and 4 <= effective_step <= 7
            and intent in (Intent.POSITIVE, Intent.READY)
            and (
                is_short_affirmative(text)
                or is_registration_confirmed(text)
                or is_affirmative_to_deposit_check(
                    text, op_outgoing, geo=geo
                )
            )
            and reg_link_sent_in_history(op_outgoing, geo=geo)
            and not script_sent_in_history(
                op_outgoing, script_ui_snippet(deposit_script_key(geo), geo)
            )
        ):
            keys = [deposit_script_key(geo)]
            logger.info(
                "conv=%s positive-after-link fallback keys=%s text=%r",
                conv_id[:8],
                keys,
                (text or "")[:40],
            )
        elif (
            geo == "cm"
            and 3 <= effective_step < 6
            and is_deposit_tier_choice(text, geo=geo)
            and not reg_link_sent_in_history(op_outgoing, geo=geo)
        ):
            keys = ["05_registration", "06_link", "07_chrome"]
            logger.info(
                "conv=%s CM tier-choice fallback keys=%s text=%r",
                conv_id[:8],
                keys,
                (text or "")[:40],
            )
        elif (
            geo in ("zm", "dj")
            and 2 <= effective_step < 6
            and is_deposit_tier_choice(text, geo=geo)
            and not reg_link_sent_in_history(op_outgoing, geo=geo)
        ):
            keys = ["04_registration", "05_link"]
            logger.info(
                "conv=%s tier-choice fallback keys=%s text=%r",
                conv_id[:8],
                keys,
                (text or "")[:40],
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
    if not keys and geo in ("zm", "dj", "cm") and is_no_status(conv):
        keys = resolve_zm_backlog_fallback(
            effective_step, op_outgoing, intent.value, geo=geo, text=text
        )
        if keys:
            logger.info(
                "conv=%s ZM backlog fallback eff_step=%s keys=%s",
                conv_id[:8],
                effective_step,
                keys,
            )

    if (
        not keys
        and needs_reply
        and geo in ("zm", "dj", "cm")
        and 2 <= effective_step < 6
        and not reg_link_sent_in_history(op_outgoing, geo=geo)
    ):
        for m in _recent_incoming_messages(msg_only):
            tier_text = (m.get("text") or "").strip()
            if is_deposit_tier_choice(tier_text):
                keys = ["04_registration", "05_link"]
                msg_id = str(m.get("id") or "")
                logger.info(
                    "conv=%s tier-in-thread keys=%s text=%r",
                    conv_id[:8],
                    keys,
                    tier_text[:20],
                )
                break

    if (
        needs_reply
        and not deposit_signal
        and not keys
        and llm_router_enabled()
    ):
        llm = await route_funnel_message(
            geo=geo,
            text=text or ("(photo)" if has_real_image else ""),
            effective_step=effective_step,
            rule_intent=intent.value,
            outgoing_texts=op_outgoing,
            folder=folder,
            has_image=has_real_image,
            reg_link_sent=reg_link_sent_in_history(op_outgoing, geo=geo),
            deposit_script_sent=script_sent_in_history(
                op_outgoing, script_ui_snippet(deposit_script_key(geo), geo)
            ),
        )
        if llm:
            if llm_router_mode() == "learn":
                logger.info(
                    "LLM learn observe conv=%s geo=%s action=%s keys=%s "
                    "conf=%.2f intent=%s note=%r",
                    conv_id[:8],
                    geo,
                    llm.action,
                    llm.script_keys,
                    llm.confidence,
                    llm.intent,
                    (llm.note or "")[:80],
                )
            elif llm.confidence >= 0.55 and llm_router_may_send():
                if llm_router_strict():
                    if llm.action in ("pause", "escalate") or llm.escalate:
                        logger.info(
                            "conv=%s LLM strict — ignore %s",
                            conv_id[:8],
                            llm.action,
                        )
                    elif llm.action == "wait":
                        pass
                    elif llm.script_keys:
                        keys = llm.script_keys
                        logger.info(
                            "conv=%s LLM route keys=%s conf=%.2f",
                            conv_id[:8],
                            keys,
                            llm.confidence,
                        )
                elif llm.action == "pause":
                    await db.save_conversation_state(
                        account_id,
                        conv_id,
                        pause_scripts=1,
                        last_processed_msg_id=msg_id,
                    )
                    return "paused"
                elif llm.action == "wait":
                    pass
                elif llm.escalate or llm.action == "escalate":
                    await _escalate_once(
                        bot,
                        esc_chat,
                        account_id=account_id,
                        conv_id=conv_id,
                        msg_id=msg_id,
                        state=state,
                        account=account,
                        channel_id=channel_id,
                        title="LLM → оператор",
                        client_name=client_name,
                        channel_name=channel_name,
                        folder=folder,
                        reason=llm.escalate_reason or llm.note or "LLM escalate",
                        last_message=text or "(photo)",
                        pause=True,
                    )
                    return True
                elif llm.script_keys:
                    keys = llm.script_keys
                    logger.info(
                        "conv=%s LLM route keys=%s conf=%.2f",
                        conv_id[:8],
                        keys,
                        llm.confidence,
                    )

    if needs_reply and not deposit_signal and not keys:
        if (
            intent in (Intent.QUESTION, Intent.UNKNOWN)
            and not funnel_active
            and needs_human_for_text(
                intent, step, text, no_status=no_status, geo=geo
            )
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
        elif geo in ("zm", "dj", "cm") and is_no_status(conv):
            keys = resolve_zm_backlog_fallback(
                effective_step, op_outgoing, intent.value, geo=geo, text=text
            )
        if not keys:
            await db.save_conversation_state(
                account_id, conv_id, last_processed_msg_id=msg_id
            )
            return "done"

    keys = filter_auto_script_keys(keys)

    if keys and reg_link_sent_in_history(op_outgoing, geo=geo):
        if is_registration_confirmed(text) or intent == Intent.JOINED:
            keys = [
                k
                for k in keys
                if k
                not in (
                    "01_intro",
                    "02_how_it_works",
                    "03_zmw_table",
                    "04_registration",
                    "05_link",
                )
            ]
            if should_send_deposit_script(
                text, effective_step, op_outgoing, folder_step=folder_step, geo=geo
            ):
                keys = ["06_deposit"]

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
            geo=geo,
        )
        actions_sent = True

    new_step = step
    reg_keys = reg_script_keys_set(geo)
    explain_keys = (
        {"02_age", "03_steps", "04_tier"}
        if geo == "cm"
        else {"02_how_it_works", "03_zmw_table"}
    )
    reg_handoff = geo in ("zm", "eg", "dj", "cm") and bool(reg_keys.intersection(keys))
    if reg_handoff:
        new_step = 4
        if pager_user_id:
            send_buf.queue_status_patch(
                conv_id, funnel_statuses["in_progress"]
            )
    elif explain_keys.intersection(keys):
        new_step = max(new_step, 2 if geo == "eg" else 3)
    elif keys == ["01_intro"]:
        new_step = max(new_step, 1)
    elif deposit_script_key(geo) in keys:
        new_step = max(new_step, 7)
        if pager_user_id:
            send_buf.queue_status_patch(
                conv_id, funnel_statuses["wait_id"]
            )
    elif game_id_script_key(geo) in keys:
        new_step = max(new_step, 6)
        if pager_user_id:
            send_buf.queue_status_patch(
                conv_id, funnel_statuses["wait_id"]
            )

    if actions_sent:
        commit: dict[str, Any] = {
            "step": new_step,
            "last_processed_msg_id": msg_id,
        }
        if reg_handoff:
            commit["pause_scripts"] = 0
            commit["human_takeover"] = 0
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


async def _process_account(bot: Bot, account: dict[str, Any]) -> int:
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
                return 0
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
        if not client.org_id:
            await client.resolve_org_id_live()
        session_ok = False
        for attempt in range(2):
            try:
                await client.list_conversations(page_size=1)
                session_ok = True
                break
            except PagerAPIError as exc:
                if is_org_id_error(exc):
                    await client.resolve_org_id_live()
                    if client.org_id:
                        client.org_id_fallback = client.org_id
                        client.cookies["_pager_org_id"] = client.org_id
                    if attempt == 0:
                        await client.warm_session()
                        continue
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
                return 0
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
                return 0
        else:
            _session_cache[account_id] = (time.time(), dict(client.cookies))

        enabled = await _ensure_enabled_channels(account_id, client)
        ch_rows = await db.list_channels(account_id)
        all_channel_ids = {
            str(c.get("channel_id") or "").strip()
            for c in ch_rows
            if c.get("channel_id")
        }
        learn_active = (
            llm_router_mode() == "learn"
            and bool(resolve_llm_api_key())
            and learn_account_allowed(account)
        )

        if enabled is None and not all_channel_ids:
            logger.warning(
                "Worker account=%s: no channels — refresh in Telegram bot",
                account_id,
            )
            return 0
        if not enabled and not learn_active:
            logger.warning(
                "Worker account=%s: no enabled channels",
                account_id,
            )
            return 0
        if not enabled and learn_active:
            logger.info(
                "Worker account=%s: learn-only — %s channel(s), "
                "auto-reply off, scanning Завершено/Депи не дошли",
                account_id,
                len(all_channel_ids),
            )

        ch_names = {
            str(c.get("channel_id") or ""): str(c.get("name") or "")
            for c in ch_rows
        }
        cmap = account.get("_channel_geo") or await db.get_channel_geo_map(
            account_id, account_geo=str(account.get("geo") or "zm")
        )
        for cid in sorted(enabled or all_channel_ids):
            tag = "enabled" if enabled and cid in enabled else "learn"
            logger.info(
                "Worker account=%s: %s channel %s name=%r geo=%s",
                account_id,
                tag,
                cid[:8],
                ch_names.get(cid, "?")[:32],
                resolve_conv_geo({**account, "_channel_geo": cmap}, cid),
            )
        await client.warm_session()
        if not client.session_user_id:
            uid = await client.resolve_session_user_id()
            if not uid and _settings.pager_user_id:
                client.session_user_id = _settings.pager_user_id

        channel_folders: dict[str, set[str]] = {}
        if enabled:
            channel_folders = await db.build_channel_folders_map(account_id, enabled)
        status_rows = await db.list_statuses(account_id)
        if not status_rows:
            try:
                api_st = await client.list_statuses_api()
                if api_st:
                    await db.sync_statuses(account_id, api_st)
                    status_rows = await db.list_statuses(account_id)
            except Exception as exc:
                logger.debug(
                    "Worker account=%s: status sync skipped: %s",
                    account_id,
                    exc,
                )
        funnel_statuses = resolve_funnel_statuses(status_rows)
        account["_funnel_statuses"] = funnel_statuses
        name_by_id: dict[str, str] = {}
        if status_rows:
            name_by_id = {
                str(s.get("status_id") or ""): str(s.get("name") or "")
                for s in status_rows
            }
            logger.info(
                "Worker account=%s: funnel map completed=%r deps_pending=%r wait_id=%r",
                account_id,
                name_by_id.get(str(funnel_statuses.get("completed") or ""), "?"),
                name_by_id.get(str(funnel_statuses.get("deps_pending") or ""), "?"),
                name_by_id.get(str(funnel_statuses.get("wait_id") or ""), "?"),
            )
        account["_channel_geo"] = await db.get_channel_geo_map(
            account_id, account_geo=str(account.get("geo") or "zm")
        )
        if channel_folders:
            sample = next(iter(channel_folders.values()), set()) or set()
            specific, all_inbox = normalize_enabled_folders(sample)
            logger.info(
                "Worker account=%s: enabled folders raw=%s effective=%s all_inbox=%s names=%s",
                account.get("id"),
                sorted(sample)[:8],
                sorted(specific)[:8],
                all_inbox,
                [
                    name_by_id.get(s, "Без статусу" if s == "" else s[:8])
                    for s in sorted(specific)
                ],
            )

        convs: list[dict] = []
        if enabled:
            list_pages = min(16, 10 + 3 * len(enabled))
            try:
                convs = await client.collect_conversations(
                    enabled,
                    max_pages=list_pages,
                    geo=db.normalize_channel_geo(str(account.get("geo") or "zm")),
                    channel_geo_map=account.get("_channel_geo") or {},
                    channel_folders=channel_folders,
                    funnel_statuses=funnel_statuses,
                )
                collected_by_ch = Counter(
                    str(c.get("channelId") or "") for c in convs
                )
                for cid in sorted(enabled):
                    geo = resolve_conv_geo(account, cid)
                    n = collected_by_ch.get(cid, 0)
                    level = logger.warning if n == 0 else logger.info
                    level(
                        "Worker account=%s: collected channel=%s name=%r geo=%s convs=%s",
                        account_id,
                        cid[:8],
                        ch_names.get(cid, "?")[:32],
                        geo,
                        n,
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
                    return 0
                acc = await db.get_account_by_tg(int(account["tg_user_id"]))
                if acc:
                    account.update(acc)
                    account["_channel_geo"] = await db.get_channel_geo_map(
                        account_id, account_geo=str(account.get("geo") or "zm")
                    )
                client = _make_client(fresh)
                await client.warm_session()
                channel_folders = await db.build_channel_folders_map(
                    account_id, enabled
                )
                convs = await client.collect_conversations(
                    enabled,
                    max_pages=list_pages,
                    geo=db.normalize_channel_geo(str(account.get("geo") or "zm")),
                    channel_geo_map=account.get("_channel_geo") or {},
                    channel_folders=channel_folders,
                    funnel_statuses=funnel_statuses,
                )

        if learn_active and all_channel_ids:
            try:
                recorded = await learn_scan_completed_chats(
                    account_id=account_id,
                    client=client,
                    scan_channels=all_channel_ids,
                    convs=convs,
                    funnel_statuses=funnel_statuses,
                    resolve_geo=lambda ch: resolve_conv_geo(account, ch),
                    max_per_cycle=int(
                        os.getenv("PAGER_LEARN_SCAN_PER_CYCLE", "24") or "24"
                    ),
                )
                if recorded > 0 and learn_notify_enabled():
                    esc = _escalation_chat(account)
                    if esc:
                        try:
                            body = await format_learn_feedback(
                                account_id,
                                email=str(account.get("email") or ""),
                            )
                            await bot.send_message(
                                esc,
                                f"📚 <b>+{recorded}</b> новых примеров за цикл\n\n{body}",
                                parse_mode="HTML",
                            )
                        except Exception:
                            logger.exception(
                                "Worker account=%s: learn notify failed",
                                account_id,
                            )
            except Exception:
                logger.exception(
                    "Worker account=%s: learn scan failed", account_id
                )

        if not enabled:
            return 0

        completed_sid = str(funnel_statuses.get("completed") or "").strip()
        inbound_convs = [
            c
            for c in convs
            if _is_incoming_direction(str(c.get("lastMessageDirection") or ""))
            or str(c.get("statusId") or "").strip() in funnel_status_ids(funnel_statuses)
            or (completed_sid and str(c.get("statusId") or "").strip() == completed_sid)
        ]

        no_status_count = sum(1 for c in inbound_convs if is_no_status(c))
        if inbound_convs:
            logger.info(
                "Worker account=%s: need_reply=%s (collected=%s) no_status=%s funnel=%s",
                account.get("id"),
                len(inbound_convs),
                len(convs),
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

        state_map = await db.load_conversation_states_map(account_id)
        allowed_folders = await db.get_account_enabled_folders(account_id)
        cycle_ctx = _AccountCycleCtx(
            account_id=account_id,
            enabled=enabled,
            allowed_folders=allowed_folders,
            state_map=state_map,
        )

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

        def _priority(conv: dict) -> tuple[int, float]:
            cid = str(conv.get("id") or "")
            st = state_map.get(cid) or db.default_conversation_state(account_id, cid)
            ts = _last_msg_ts(conv)
            fails = int(st.get("send_failures") or 0)
            st_step = int(st.get("step") or 0)
            status_id = str(conv.get("statusId") or "").strip()
            incoming = _is_incoming_direction(
                str(conv.get("lastMessageDirection") or "")
            )
            if not incoming:
                return (5, ts)
            if status_id == funnel_statuses.get("in_progress") and (
                st.get("pause_scripts") or st.get("last_escalation_msg_id")
            ):
                return (-3, -ts + fails * 1e-9)
            if status_id == funnel_statuses.get("registration") and incoming:
                return (-4, -ts + fails * 1e-9)
            if status_id == funnel_statuses.get("in_progress") and incoming:
                return (-4, -ts + fails * 1e-9)
            if status_id in (
                funnel_statuses.get("wait_id"),
                funnel_statuses.get("registration"),
                funnel_statuses.get("in_progress"),
            ):
                return (-3, -ts + fails * 1e-9)
            if st.get("human_takeover"):
                return (4, ts)
            if st.get("pause_scripts") and st_step < 4:
                return (4, ts)
            if is_no_status(conv) or status_id in funnel_status_ids(
                funnel_statuses
            ):
                if incoming:
                    return (-6, -ts + fails * 1e-9)
                return (-2, ts + fails * 1e-6)
            return (1, ts)

        scored: list[tuple[tuple[int, float], dict]] = [
            (_priority(c), c) for c in inbound_convs
        ]
        scored.sort(key=lambda x: (x[0][0], x[0][1]))
        limits = _compute_cycle_limits(
            len(inbound_convs), no_status_count, len(enabled)
        )
        max_handle = limits["max_handle"]
        max_plans = limits["max_plans"]
        funnel_cap = limits["funnel_cap"]
        batch_chunk = limits["batch_chunk"]
        browser_parallel = limits["browser_parallel"]
        plan_parallel = limits["plan_parallel"]
        process_order = _build_process_order(
            scored,
            limit=max_handle,
            no_status_n=no_status_count,
            queue_n=len(inbound_convs),
        )

        inbound = len(process_order)
        skipped = {"paused": 0, "done": 0, "no_script": 0}
        no_status_n = sum(1 for c in inbound_convs if is_no_status(c))
        n_enabled = len(enabled)
        pager_user_id = resolve_account_operator_id(
            account, cookies, org_slug=org_slug
        )
        logger.info(
            "Worker account=%s: plan budget=%s funnel_cap=%s batch=%s parallel=%s "
            "plan_parallel=%s max_handle=%s work_queue=%s (no_status=%s)",
            account.get("id"),
            max_plans,
            funnel_cap,
            batch_chunk,
            browser_parallel,
            plan_parallel,
            max_handle,
            inbound,
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
        plan_sem = asyncio.Semaphore(plan_parallel)

        async def _plan_conv(conv: dict) -> None:
            nonlocal planned
            conv_id = str(conv.get("id") or "")
            async with plan_sem:
                try:
                    result = await _handle_conversation(
                        bot,
                        account,
                        conv,
                        client,
                        send_buf,
                        cycle_ctx,
                    )
                except PagerAPIError as exc:
                    logger.warning(
                        "conv API error account=%s conv=%s: %s",
                        account.get("id"),
                        conv_id,
                        exc,
                    )
                    return
                except Exception as exc:
                    logger.warning(
                        "conv plan failed account=%s conv=%s: %s",
                        account.get("id"),
                        conv_id,
                        exc,
                    )
                    return
            if result == "paused":
                skipped["paused"] += 1
            elif result == "done":
                skipped["done"] += 1
            elif result == "no_script":
                skipped["no_script"] += 1
            elif result:
                planned += 1
            if result is True and pager_user_id:
                try:
                    await client.mark_conversation_read(
                        conv_id, user_id=pager_user_id
                    )
                except Exception:
                    pass

        for start in range(0, len(process_order), plan_parallel):
            batch = process_order[start : start + plan_parallel]
            await asyncio.gather(*(_plan_conv(c) for c in batch))

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
        return len(inbound_convs)
    except PagerAPIError as exc:
        if is_session_error(exc):
            await db.upsert_account(
                int(account["tg_user_id"]),
                session_ok=0,
                last_error="Session expired — reconnect in bot",
            )
        logger.warning("Pager API account=%s: %s", account.get("id"), exc)
    return 0


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

            max_queue = 0
            for acc in accounts:
                try:
                    q = int(
                        await asyncio.wait_for(
                            _process_account(bot, acc),
                            timeout=900.0,
                        )
                        or 0
                    )
                    max_queue = max(max_queue, q)
                except asyncio.TimeoutError:
                    logger.error(
                        "Worker account=%s: cycle timeout 900s",
                        acc.get("id"),
                    )
                except Exception:
                    logger.exception(
                        "Worker account=%s failed",
                        acc.get("id"),
                    )
        except Exception:
            logger.exception("worker tick failed")
            poll_wait = settings.poll_sec
        else:
            if max_queue > 1500:
                poll_wait = min(settings.poll_sec, 2.0)
            elif max_queue > 800:
                poll_wait = min(settings.poll_sec, 4.0)
            elif max_queue > 200:
                poll_wait = min(settings.poll_sec, 6.0)
            else:
                poll_wait = settings.poll_sec
            if max_queue > 200:
                logger.info(
                    "Worker adaptive poll=%ss (queue=%s)",
                    poll_wait,
                    max_queue,
                )
        await asyncio.sleep(poll_wait)


def start_worker(bot: Bot) -> asyncio.Task:
    global _worker_task
    if _worker_task and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(worker_loop(bot))
    return _worker_task
