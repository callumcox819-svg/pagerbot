"""Learn from successful funnel chats — deposit profile / game ID screenshots."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Callable

import database as db
from config import load_settings
from services.image_extract import (
    analyze_success_screenshot,
    extract_id_from_image_url,
    looks_like_game_id,
)
from services.llm_client import llm_router_mode, resolve_llm_api_key
from services.pager_api import PagerClient
from services.status_ids import is_learn_success_conv

logger = logging.getLogger(__name__)
_settings = load_settings()

_BALANCE_RE = re.compile(
    r"\b(\d[\d\s.,]{0,12})\s*(XAF|ZMW|EGP|USD|FCFA|CFA|K)\b",
    re.I,
)

_LEARN_FOLDER_KEYS = ("completed", "wait_id", "win")


def _is_incoming(msg: dict[str, Any]) -> bool:
    d = str(msg.get("messageDirection") or "").lower()
    return d in ("incoming", "in", "from_client", "client")


def _image_urls(msg: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict) or att.get("type") != "image":
            continue
        url = str((att.get("payload") or {}).get("url") or "").strip()
        if url:
            out.append(url)
    return out


def _success_from_analysis(
    analysis: dict[str, Any],
    *,
    gid: str,
    geo: str,
) -> bool:
    if analysis.get("is_success") is True:
        return True
    kind = str(analysis.get("kind") or "").lower()
    balance = str(analysis.get("balance") or "").strip()
    if kind in ("deposit_profile", "deposit", "payment_receipt") and balance:
        return True
    if gid and looks_like_game_id(gid, geo=geo):
        return True
    if balance and _BALANCE_RE.search(balance):
        return True
    return False


async def record_live_learn_success(
    account_id: int,
    conv_id: str,
    *,
    message_id: str,
    geo: str,
    game_id: str = "",
    balance_text: str = "",
    screenshot_kind: str = "live",
    client_name: str = "",
    folder: str = "",
    note: str = "",
) -> None:
    """Record success during live processing (all GEOs, learn mode only)."""
    if llm_router_mode() != "learn":
        return
    if not message_id or not conv_id:
        return
    if await db.learn_success_exists(account_id, conv_id, message_id):
        return
    await db.save_learn_success(
        account_id,
        conv_id,
        message_id=message_id,
        geo=geo,
        game_id=game_id,
        balance_text=balance_text,
        screenshot_kind=screenshot_kind,
        client_name=client_name,
        folder=folder,
        note=note,
    )
    logger.info(
        "LLM learn success live conv=%s geo=%s client=%r balance=%r gid=%s folder=%r",
        conv_id[:8],
        geo,
        (client_name or "")[:24],
        (balance_text or "")[:32],
        game_id or "-",
        (folder or "")[:20],
    )


async def _scan_conv_for_success(
    *,
    account_id: int,
    client: PagerClient,
    conv: dict,
    resolve_geo: Callable[[str], str],
    api_key: str,
) -> int:
    conv_id = str(conv.get("id") or "")
    if not conv_id:
        return 0
    ch = str(conv.get("channelId") or "").strip()
    geo = resolve_geo(ch)
    client_name = ((conv.get("client") or {}).get("name") or "Client").strip()
    folder = ((conv.get("status") or {}).get("name") or "").strip()

    try:
        messages = await client.list_messages(conv_id, page_size=80)
    except Exception:
        logger.debug("learn scan messages failed conv=%s", conv_id[:8])
        return 0

    msg_only = [
        m for m in messages if m.get("text") is not None or m.get("attachments")
    ]
    msg_only.sort(key=lambda m: m.get("createdAt") or "")

    recorded = 0
    for m in reversed(msg_only):
        if not _is_incoming(m):
            continue
        msg_id = str(m.get("id") or "")
        urls = _image_urls(m)
        if not urls:
            continue
        if await db.learn_success_exists(account_id, conv_id, msg_id):
            continue

        for url in urls:
            analysis = await analyze_success_screenshot(url, api_key, geo=geo)
            gid = str(analysis.get("game_id") or "").strip()
            if not gid:
                gid = await extract_id_from_image_url(url, api_key, geo=geo)
            if not _success_from_analysis(analysis, gid=gid, geo=geo):
                continue

            balance = str(analysis.get("balance") or "").strip()
            kind = str(analysis.get("kind") or "deposit_profile").strip()
            note = str(analysis.get("note") or "").strip()

            await db.save_learn_success(
                account_id,
                conv_id,
                message_id=msg_id,
                geo=geo,
                game_id=gid,
                balance_text=balance,
                screenshot_kind=kind,
                client_name=client_name,
                folder=folder,
                note=note,
            )
            logger.info(
                "LLM learn success scan conv=%s geo=%s client=%r "
                "balance=%r gid=%s kind=%s folder=%r",
                conv_id[:8],
                geo,
                client_name[:24],
                balance[:32],
                gid or "-",
                kind,
                folder[:20],
            )
            recorded += 1
            break
        if recorded and msg_id:
            break
    return recorded


def _fair_sample_by_channel(
    candidates: list[dict], *, max_total: int
) -> list[dict]:
    """Spread learn scan across channels (ZM / EG / CM Brice / CM Ndzié, etc.)."""
    by_ch: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        ch = str(c.get("channelId") or "").strip() or "_"
        by_ch[ch].append(c)
    if not by_ch:
        return []
    per_ch = max(1, max_total // len(by_ch))
    out: list[dict] = []
    for ch in sorted(by_ch.keys()):
        out.extend(by_ch[ch][:per_ch])
    return out[: max(1, max_total)]


async def learn_scan_completed_chats(
    *,
    account_id: int,
    client: PagerClient,
    enabled_channels: set[str],
    convs: list[dict],
    funnel_statuses: dict[str, str],
    resolve_geo: Callable[[str], str],
    max_per_cycle: int = 24,
) -> int:
    """Scan success folders per channel/geo — read-only learning."""
    if llm_router_mode() != "learn":
        return 0
    api_key = resolve_llm_api_key()
    if not api_key:
        return 0

    seen: dict[str, dict] = {}
    for c in convs:
        if is_learn_success_conv(c, funnel_statuses):
            cid = str(c.get("id") or "")
            if cid:
                seen[cid] = c

    for ch in sorted(enabled_channels):
        for folder_key in _LEARN_FOLDER_KEYS:
            sid = str(funnel_statuses.get(folder_key) or "").strip()
            if not sid:
                continue
            for page in range(1, 4):
                try:
                    batch = await client.list_conversations(
                        page=page,
                        page_size=50,
                        channel_id=ch,
                        status_id=sid,
                    )
                except Exception:
                    break
                if not batch:
                    break
                for c in batch:
                    if not is_learn_success_conv(c, funnel_statuses):
                        continue
                    cid = str(c.get("id") or "")
                    if cid:
                        seen[cid] = c

    candidates = list(seen.values())
    sample = _fair_sample_by_channel(candidates, max_total=max_per_cycle)

    recorded = 0
    for conv in sample:
        recorded += await _scan_conv_for_success(
            account_id=account_id,
            client=client,
            conv=conv,
            resolve_geo=resolve_geo,
            api_key=api_key,
        )

    if candidates:
        by_geo = await db.count_learn_successes_by_geo(account_id)
        logger.info(
            "LLM learn scan account=%s candidate_chats=%s scanned=%s "
            "recorded=%s totals_by_geo=%s",
            account_id,
            len(candidates),
            len(sample),
            recorded,
            by_geo or {},
        )
    return recorded
