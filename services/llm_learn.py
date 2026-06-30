"""Learn from funnel folders — deposit profile / game ID screenshots."""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from typing import Any, Callable

import database as db
from config import SCRIPTS_DIR, load_settings
from services.image_extract import (
    analyze_success_screenshot,
    extract_id_from_image_url,
    looks_like_game_id,
)
from services.llm_client import llm_router_mode, resolve_llm_api_key
from services.pager_api import PagerClient
from services.script_engine import (
    infer_step_from_thread,
    script_sent_in_history,
    script_ui_snippet,
)
from services.status_ids import is_deps_pending_conv, is_learn_folder_conv

logger = logging.getLogger(__name__)
_settings = load_settings()

_BALANCE_RE = re.compile(
    r"\b(\d[\d\s.,]{0,12})\s*(XAF|ZMW|EGP|USD|FCFA|CFA|K)\b",
    re.I,
)

_LEARN_FOLDER_KEYS = ("completed", "deps_pending", "wait_id", "win")

_GEO_LABELS = {
    "zm": "Zambia",
    "eg": "Egypt",
    "cm": "Cameroon",
    "dj": "Djibouti",
}


def learn_account_email() -> str:
    """Pager email used for AI learning (default: harley account)."""
    return (
        os.getenv("PAGER_LEARN_EMAIL") or "2harleydewidson@gmail.com"
    ).strip().lower()


def learn_account_allowed(account: dict[str, Any]) -> bool:
    want = learn_account_email()
    if want in ("", "*", "all"):
        return True
    return (str(account.get("email") or "").strip().lower()) == want


def _outgoing_texts(messages: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if str(m.get("messageDirection") or "").lower() not in ("outgoing", "out"):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if not (m.get("isDelivered") or m.get("facebookMessageId")):
            continue
        out.append(text)
    return out


def _scripts_delivered_keys(outgoing: list[str], geo: str) -> list[str]:
    g = (geo or "zm").strip().lower()
    root = SCRIPTS_DIR / g
    if not root.is_dir():
        return []
    keys: list[str] = []
    for path in sorted(root.glob("*.txt")):
        stem = path.stem
        sn = script_ui_snippet(stem, g)
        if sn and script_sent_in_history(outgoing, sn):
            keys.append(stem)
    extras = root / "extras"
    if extras.is_dir():
        for path in sorted(extras.glob("*.txt")):
            stem = f"extras/{path.stem}"
            sn = script_ui_snippet(path.stem, g)
            if sn and script_sent_in_history(outgoing, sn):
                keys.append(stem)
    return keys


def _client_text_turns(messages: list[dict[str, Any]], *, limit: int = 6) -> list[str]:
    turns: list[str] = []
    for m in messages:
        if not _is_incoming(m):
            continue
        text = (m.get("text") or "").strip()
        if text:
            turns.append(text[:100])
        elif m.get("attachments"):
            turns.append("(photo)")
    return turns[-limit:]


def _geo_from_transcript(blob: str, fallback: str = "zm") -> str:
    b = (blob or "").lower()
    if "camerun" in b or "cash056" in b or " xaf" in b or "fcfa" in b:
        return "cm"
    if "egypt" in b or "egp" in b or "egypt0011" in b:
        return "eg"
    if "djibouti" in b or "bji777" in b:
        return "dj"
    if "zam577" in b or "zmw" in b:
        return "zm"
    return (fallback or "zm").strip().lower() or "zm"


def _build_transcript(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in sorted(messages, key=lambda x: str(x.get("createdAt") or "")):
        d = str(m.get("messageDirection") or "").lower()
        if d in ("incoming", "in", "from_client", "client"):
            role = "CLIENT"
        elif d in ("outgoing", "out"):
            role = "OPERATOR"
        else:
            continue
        text = (m.get("text") or "").strip()
        if not text and m.get("attachments"):
            text = "(photo)"
        if not text:
            continue
        lines.append(f"{role}: {text[:400]}")
    return "\n".join(lines)[:8000]


def learn_screenshots_enabled() -> bool:
    return os.getenv("PAGER_LEARN_SCREENSHOTS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


async def _summarize_dialog(
    transcript: str, *, api_key: str, folder: str, client_name: str
) -> str:
    if not api_key or len(transcript) < 40:
        return ""
    from services.llm_client import chat_completion

    summary = await chat_completion(
        [
            {
                "role": "user",
                "content": (
                    f"Successful 1xBet funnel chat, folder «{folder}», client {client_name}.\n"
                    "In 2-3 short sentences: how did the operator guide the client, "
                    "what did the client say/do, what was the outcome?\n"
                    "Do NOT invent links, promo codes, or amounts.\n\n"
                    f"{transcript[:3500]}"
                ),
            }
        ],
        api_key=api_key,
        max_tokens=150,
    )
    return (summary or "").strip()[:500]


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


def _folder_is_deps_pending(folder: str) -> bool:
    n = (folder or "").strip().lower()
    if not n:
        return False
    return is_deps_pending_conv({"status": {"name": folder}, "statusId": ""})


def _worth_recording(
    analysis: dict[str, Any],
    *,
    gid: str,
    geo: str,
    folder: str,
) -> bool:
    if analysis.get("is_success") is True:
        return True
    kind = str(analysis.get("kind") or "").lower()
    balance = str(analysis.get("balance") or "").strip()
    note = str(analysis.get("note") or "").strip()
    if kind in ("deposit_profile", "deposit", "payment_receipt") and balance:
        return True
    if gid and looks_like_game_id(gid, geo=geo):
        return True
    if balance and _BALANCE_RE.search(balance):
        return True
    if _folder_is_deps_pending(folder):
        if kind in (
            "deposit_profile",
            "deposit",
            "payment_receipt",
            "game_id",
            "link_error",
            "registration",
        ):
            return True
        if note or balance or gid:
            return True
    return False


async def format_learn_feedback(
    account_id: int,
    *,
    email: str = "",
    recent_limit: int = 8,
    account_meta: dict | None = None,
) -> str:
    """Human-readable training report for Telegram."""
    total = await db.count_learn_successes(account_id)
    global_total = await db.count_learn_successes(None)
    by_geo = await db.count_learn_successes_by_geo(account_id)
    global_by_geo = await db.count_learn_successes_by_geo(None)
    by_folder = await db.count_learn_successes_by_folder(account_id)
    recent = await db.list_learn_recent(account_id, limit=recent_limit)
    mode = llm_router_mode()

    lines = ["📚 <b>AI обучение — отчёт</b>", ""]
    learn_email = learn_account_email()
    if learn_email and learn_email not in ("*", "all"):
        lines.append(f"Учится только: <code>{learn_email}</code>")
    if email:
        lines.append(f"Pager: <code>{email}</code>")
    lines.append(f"Всего примеров в базе: <b>{total}</b>")
    dialog_n = sum(
        1
        for r in await db.list_learn_recent(account_id, limit=500)
        if str(r.get("screenshot_kind") or "") in ("chat_dialog", "dialog_flow")
    )
    if dialog_n:
        lines.append(f"Успешных чатов (переписка): <b>{dialog_n}</b>")
    if global_total > total:
        lines.append(
            f"<i>На всех аккаунтах воркера: <b>{global_total}</b> "
            f"({', '.join(f'{g}={n}' for g, n in sorted(global_by_geo.items()))})</i>"
        )
    lines.append(f"Режим LLM: <code>{mode or 'off'}</code>")
    if mode == "learn":
        lines.append(
            "<i>Бот наблюдает и копит примеры — скрипты и ссылки не меняет.</i>"
        )
    lines.append("")

    if by_geo:
        lines.append("<b>По странам (GEO):</b>")
        for geo in sorted(by_geo.keys()):
            label = _GEO_LABELS.get(geo, geo.upper())
            lines.append(f"• {label} ({geo}): {by_geo[geo]} примеров")
        lines.append("")

    if by_folder:
        lines.append("<b>По папкам Pager:</b>")
        for folder, n in list(by_folder.items())[:12]:
            lines.append(f"• {folder}: {n}")
        lines.append("")

    if recent:
        lines.append(f"<b>Последние {len(recent)} примеров:</b>")
        for i, r in enumerate(recent, 1):
            geo = str(r.get("geo") or "?").upper()
            folder = str(r.get("folder") or "—")[:28]
            client = str(r.get("client_name") or "—")[:22]
            bal = str(r.get("balance_text") or "").strip()
            gid = str(r.get("game_id") or "").strip()
            kind = str(r.get("screenshot_kind") or "").strip()
            note = str(r.get("note") or "").strip()
            extras = []
            if bal:
                extras.append(f"баланс {bal}")
            if gid:
                extras.append(f"ID {gid}")
            if kind and kind != "deposit_profile":
                extras.append(kind)
            if kind == "dialog_flow" or kind == "chat_dialog":
                if note:
                    extras.append(note[:120])
                dlg = str(r.get("dialog_text") or "").strip()
                if dlg:
                    extras.append(dlg[:100].replace("\n", " / "))
            elif note:
                extras.append(note[:40])
            tail = f" — {', '.join(extras)}" if extras else ""
            lines.append(f"{i}. {geo} | {folder} | {client}{tail}")
    elif total == 0:
        chs = await db.list_channels(account_id)
        enabled_n = sum(1 for c in chs if c.get("enabled"))
        lines.append("<b>Почему пока 0:</b>")
        lines.append(
            f"• Каналов в Pager-аккаунте (для обучения): <b>{len(chs)}</b>"
        )
        lines.append(f"• Каналов для автоответа: {enabled_n}/{len(chs)}")
        meta = account_meta or {}
        if meta:
            lines.append(
                f"• Воркер: auto_reply={'✅' if meta.get('auto_reply') else '❌'} "
                f"paused={'⏸' if meta.get('paused') else '▶️'} "
                f"session={'OK' if meta.get('session_ok') else 'FAIL'}"
            )
        if not resolve_llm_api_key():
            lines.append("• ⚠️ <code>OPENROUTER_API_KEY</code> не задан на Railway")
        if len(chs) == 0:
            lines.append(
                "• ⚠️ Нет каналов — перелогиньтесь в 🔐 Pager или 📡 Каналы → 🔄"
            )
        elif mode == "learn":
            lines.append(
                "• Обучение идёт по <b>всем</b> каналам аккаунта — "
                "📂 Выбор папок и вкл/выкл каналов не нужны"
            )
            if len(chs) < 5:
                lines.append(
                    "• ⚠️ Мало каналов в боте — 📡 Каналы → <b>🔄 Обновить</b> "
                    "(на главном Pager их обычно больше)"
                )
            lines.append(
                "• Скан идёт — если 0 долго: скрины не скачиваются с Facebook CDN"
            )
        lines.append(
            "<i>Примеры привязаны к этому Pager-аккаунту.</i>"
        )

    lines.append("")
    lines.append(
        "Папки: <b>Завершено</b>, <b>Депи не дошли</b>, <b>Чекаю ID</b> — "
        "бот читает всю переписку OPERATOR/CLIENT."
    )
    lines.append("Обновить отчёт: /learn_stats")
    return "\n".join(lines)


def learn_notify_enabled() -> bool:
    return os.getenv("PAGER_LEARN_NOTIFY", "1").strip().lower() in (
        "1",
        "true",
        "yes",
    )


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
        messages = await client.list_messages(conv_id, page_size=120)
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
            cookies = getattr(client, "cookies", None)
            analysis = await analyze_success_screenshot(
                url, api_key, geo=geo, cookies=cookies
            )
            gid = str(analysis.get("game_id") or "").strip()
            if not gid:
                gid = await extract_id_from_image_url(
                    url, api_key, geo=geo, cookies=cookies
                )
            if not _worth_recording(analysis, gid=gid, geo=geo, folder=folder):
                logger.debug(
                    "LLM learn skip conv=%s geo=%s folder=%r kind=%s balance=%r gid=%s",
                    conv_id[:8],
                    geo,
                    folder[:20],
                    analysis.get("kind"),
                    str(analysis.get("balance") or "")[:16],
                    gid or "-",
                )
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


async def _scan_conv_for_dialog(
    *,
    account_id: int,
    client: PagerClient,
    conv: dict,
    funnel_statuses: dict[str, str],
    resolve_geo: Callable[[str], str],
    api_key: str,
) -> int:
    """Read full chat from success folders — how operator talks to client."""
    conv_id = str(conv.get("id") or "")
    if not conv_id:
        return 0
    dlg_msg_id = f"dialog:{conv_id}"
    if await db.learn_success_exists(account_id, conv_id, dlg_msg_id):
        return 0

    if not is_learn_folder_conv(conv, funnel_statuses):
        return 0

    ch = str(conv.get("channelId") or "").strip()
    channel_geo = resolve_geo(ch)
    client_name = ((conv.get("client") or {}).get("name") or "Client").strip()
    folder = ((conv.get("status") or {}).get("name") or "").strip()

    try:
        messages = await client.list_messages(conv_id, page_size=200)
    except Exception:
        logger.warning("learn dialog messages failed conv=%s", conv_id[:8])
        return 0

    transcript = _build_transcript(messages)
    if not transcript:
        logger.debug("learn dialog empty transcript conv=%s folder=%r", conv_id[:8], folder[:20])
        return 0

    client_lines = sum(1 for ln in transcript.splitlines() if ln.startswith("CLIENT:"))
    op_lines = sum(1 for ln in transcript.splitlines() if ln.startswith("OPERATOR:"))
    if client_lines < 1 or op_lines < 1:
        return 0

    geo = _geo_from_transcript(transcript, channel_geo)
    outgoing = _outgoing_texts(messages)
    scripts: list[str] = []
    for g in ("cm", "eg", "zm", "dj"):
        scripts = _scripts_delivered_keys(outgoing, g)
        if scripts:
            geo = g
            break
    step = infer_step_from_thread(messages, geo=geo)
    summary = await _summarize_dialog(
        transcript, api_key=api_key, folder=folder, client_name=client_name
    )
    note = summary or f"folder={folder[:32]}; step={step}; scripts={','.join(scripts[:8])}"

    await db.save_learn_success(
        account_id,
        conv_id,
        message_id=dlg_msg_id,
        geo=geo,
        game_id="",
        balance_text=f"msgs={client_lines + op_lines}",
        screenshot_kind="chat_dialog",
        client_name=client_name,
        folder=folder,
        note=note,
        dialog_text=transcript,
    )
    logger.info(
        "LLM learn dialog conv=%s geo=%s client=%r folder=%r "
        "turns=%s+%s scripts=%s",
        conv_id[:8],
        geo,
        client_name[:24],
        folder[:24],
        op_lines,
        client_lines,
        scripts[:5],
    )
    return 1


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
    scan_channels: set[str],
    convs: list[dict],
    funnel_statuses: dict[str, str],
    resolve_geo: Callable[[str], str],
    max_per_cycle: int = 40,
) -> int:
    """Scan success folders — learn from full chat dialogs."""
    if llm_router_mode() != "learn":
        return 0
    api_key = resolve_llm_api_key()
    if not api_key:
        return 0

    seen: dict[str, dict] = {}
    for c in convs:
        if is_learn_folder_conv(c, funnel_statuses):
            cid = str(c.get("id") or "")
            if cid:
                seen[cid] = c

    for ch in sorted(scan_channels):
        for folder_key in _LEARN_FOLDER_KEYS:
            sid = str(funnel_statuses.get(folder_key) or "").strip()
            if not sid:
                continue
            for page in range(1, 7):
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
                    if not is_learn_folder_conv(c, funnel_statuses):
                        continue
                    cid = str(c.get("id") or "")
                    if cid:
                        seen[cid] = c

    candidates = list(seen.values())
    sample = _fair_sample_by_channel(candidates, max_total=max_per_cycle)

    recorded = 0
    for conv in sample:
        recorded += await _scan_conv_for_dialog(
            account_id=account_id,
            client=client,
            conv=conv,
            funnel_statuses=funnel_statuses,
            resolve_geo=resolve_geo,
            api_key=api_key,
        )
        if learn_screenshots_enabled():
            recorded += await _scan_conv_for_success(
                account_id=account_id,
                client=client,
                conv=conv,
                resolve_geo=resolve_geo,
                api_key=api_key,
            )

    if candidates:
        by_geo = await db.count_learn_successes_by_geo(account_id)
        by_folder = await db.count_learn_successes_by_folder(account_id)
        logger.info(
            "LLM learn scan account=%s candidate_chats=%s scanned=%s "
            "recorded=%s totals_by_geo=%s totals_by_folder=%s",
            account_id,
            len(candidates),
            len(sample),
            recorded,
            by_geo or {},
            by_folder or {},
        )
    return recorded
