"""Geo-aware funnel router — one LLM key, all GEOs (zm / dj / eg / cm)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import SCRIPTS_DIR
import database as db
from services.llm_client import chat_completion_json, llm_router_enabled, resolve_llm_api_key
from services.script_engine import (
    deposit_script_key,
    game_id_script_key,
    link_help_script_keys,
    reg_link_script_key,
    script_sent_in_history,
    script_ui_snippet,
)

logger = logging.getLogger(__name__)

GEO_META: dict[str, dict[str, str]] = {
    "zm": {
        "label": "Zambia",
        "language": "English",
        "funnel": (
            "01_intro → 02_how_it_works → 03_zmw_table → 04_registration → "
            "05_link → 06_deposit → 07_game_id → 08_tg_invite → 09_tg_link"
        ),
    },
    "dj": {
        "label": "Djibouti",
        "language": "French",
        "funnel": (
            "01_intro → 02_how_it_works → 03_zmw_table → 04_registration → "
            "05_link → 06_deposit → 07_game_id → 08_tg_invite → 09_tg_link"
        ),
    },
    "eg": {
        "label": "Egypt",
        "language": "Arabic",
        "funnel": (
            "01_intro → 02_how_it_works → 04_registration → 05_link → "
            "06_deposit → 07_game_id → 08_tg_invite → 09_tg_link"
        ),
    },
    "cm": {
        "label": "Cameroon",
        "language": "French",
        "funnel": (
            "01_intro → 01_intro_2 → 02_age → 03_steps → 04_tier → "
            "05_registration → 06_link → 07_chrome → 09_deposit → "
            "08_game_id → 10_tg_invite → 11_tg_link"
        ),
    },
}


@dataclass
class LlmRouteDecision:
    action: str
    script_keys: list[str]
    intent: str
    escalate: bool
    escalate_reason: str
    confidence: float
    note: str


def _script_keys_for_geo(geo: str) -> list[str]:
    g = (geo or "zm").strip().lower()
    root = SCRIPTS_DIR / g
    if not root.is_dir():
        return []
    keys: list[str] = []
    for path in sorted(root.glob("*.txt")):
        keys.append(path.stem)
    extras = root / "extras"
    if extras.is_dir():
        for path in sorted(extras.glob("*.txt")):
            keys.append(f"extras/{path.stem}")
    return keys


def _scripts_delivered(outgoing_texts: list[str], geo: str) -> list[str]:
    g = (geo or "zm").strip().lower()
    delivered: list[str] = []
    for key in _script_keys_for_geo(g):
        stem = key.split("/")[-1]
        sn = script_ui_snippet(stem, g)
        if sn and script_sent_in_history(outgoing_texts, sn):
            delivered.append(key)
    link_sn = script_ui_snippet(reg_link_script_key(g), g)
    if link_sn and script_sent_in_history(outgoing_texts, link_sn):
        if reg_link_script_key(g) not in delivered:
            delivered.append(reg_link_script_key(g))
    return delivered


def _filter_valid_keys(geo: str, keys: list[str]) -> list[str]:
    allowed = set(_script_keys_for_geo(geo))
    allowed_stems = {k.split("/")[-1] for k in allowed}
    out: list[str] = []
    for raw in keys or []:
        k = str(raw or "").strip()
        if not k:
            continue
        if k in allowed:
            out.append(k)
            continue
        stem = k.split("/")[-1]
        if stem in allowed_stems:
            out.append(stem)
    return out


def _system_prompt(geo: str, learn_block: str = "") -> str:
    meta = GEO_META.get(geo, GEO_META["zm"])
    keys = ", ".join(_script_keys_for_geo(geo)[:40])
    extra = ""
    if learn_block:
        extra = f"\n{learn_block}\n"
    return (
        "You route Pager funnel chats for 1xBet acquisition bots. "
        "Reply with JSON only, no markdown.\n"
        f"GEO: {geo} ({meta['label']}). Client language: {meta['language']}.\n"
        f"Funnel order: {meta['funnel']}.\n"
        f"Allowed script_keys: {keys}.\n"
        f"{extra}"
        "Rules:\n"
        "- You ONLY choose pre-written script_keys from the list — never write message text.\n"
        "- NEVER change links, promo codes, or minimum deposit amounts "
        "(they are fixed inside script files).\n"
        "- NEVER invent URLs, codes, or sums.\n"
        "- Never skip steps that were not sent yet (check scripts_delivered).\n"
        "- If client screenshot shows broken link / site inaccessible → "
        'action "link_help" (not deposit).\n'
        "- Deposit script only after registration link was delivered AND "
        "client registered or sent payment proof.\n"
        "- If client declines / insults / scam accusation → action pause.\n"
        "- If unclear human question → action escalate.\n"
        "- game_id only in wait_id stage after deposit script.\n"
        "JSON schema:\n"
        '{"action":"send_scripts|link_help|wait|escalate|pause",'
        '"script_keys":["..."],'
        '"intent":"interested|positive|ready|question|unknown|declined|complaint|deposit_done|image_only",'
        '"escalate":false,"escalate_reason":"","confidence":0.0,"note":""}'
    )


async def _learn_examples_block(geo: str) -> str:
    if not llm_router_enabled():
        return ""
    rows = await db.list_learn_success_examples(geo, limit=6)
    if not rows:
        return ""
    lines = [
        "Successful patterns from real clients in this GEO "
        "(use only to pick script_keys — never copy links/codes/amounts):"
    ]
    for r in rows:
        parts = []
        kind = str(r.get("screenshot_kind") or "").strip()
        if kind:
            parts.append(f"kind={kind}")
        bal = str(r.get("balance_text") or "").strip()
        if bal:
            parts.append(f"balance={bal!r}")
        gid = str(r.get("game_id") or "").strip()
        if gid:
            parts.append(f"game_id={gid}")
        folder = str(r.get("folder") or "").strip()
        if folder:
            parts.append(f"folder={folder!r}")
        if parts:
            lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


async def route_funnel_message(
    *,
    geo: str,
    text: str,
    effective_step: int,
    rule_intent: str,
    outgoing_texts: list[str],
    folder: str,
    has_image: bool,
    reg_link_sent: bool,
    deposit_script_sent: bool,
) -> LlmRouteDecision | None:
    """Pick next funnel action for any GEO using one shared LLM key."""
    api_key = resolve_llm_api_key()
    if not api_key:
        return None
    g = (geo or "zm").strip().lower()
    if g not in GEO_META:
        g = "zm"

    user_payload = {
        "geo": g,
        "client_text": (text or "").strip() or "(empty)",
        "has_image": bool(has_image),
        "effective_step": int(effective_step or 0),
        "rule_intent": (rule_intent or "unknown").strip(),
        "folder": (folder or "").strip(),
        "reg_link_sent": bool(reg_link_sent),
        "deposit_script_sent": bool(deposit_script_sent),
        "scripts_delivered": _scripts_delivered(outgoing_texts, g),
        "deposit_key": deposit_script_key(g),
        "game_id_key": game_id_script_key(g),
    }

    learn_block = await _learn_examples_block(g)
    raw = await chat_completion_json(
        [
            {"role": "system", "content": _system_prompt(g, learn_block)},
            {
                "role": "user",
                "content": (
                    "Decide the next bot action for this client message:\n"
                    + json.dumps(user_payload, ensure_ascii=False)
                ),
            },
        ],
        api_key=api_key,
    )
    if not raw:
        return None

    action = str(raw.get("action") or "wait").strip().lower()
    script_keys = _filter_valid_keys(g, list(raw.get("script_keys") or []))
    intent = str(raw.get("intent") or rule_intent or "unknown").strip().lower()
    escalate = bool(raw.get("escalate"))
    escalate_reason = str(raw.get("escalate_reason") or raw.get("note") or "").strip()
    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence <= 0 and action in ("send_scripts", "link_help") and script_keys:
        confidence = 0.75
    note = str(raw.get("note") or "").strip()

    if action == "link_help":
        script_keys = link_help_script_keys(g)

    if action == "send_scripts" and not script_keys:
        action = "wait"

    decision = LlmRouteDecision(
        action=action,
        script_keys=script_keys,
        intent=intent,
        escalate=escalate or action == "escalate",
        escalate_reason=escalate_reason,
        confidence=max(0.0, min(1.0, confidence)),
        note=note,
    )
    logger.info(
        "LLM route geo=%s action=%s keys=%s conf=%.2f intent=%s",
        g,
        decision.action,
        decision.script_keys,
        decision.confidence,
        decision.intent,
    )
    return decision
