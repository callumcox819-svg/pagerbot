"""Compose client-facing text from learned chats — locked links/codes/amounts."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import database as db
from services.llm_client import (
    chat_completion_json,
    llm_compose_confidence_min,
    llm_router_assist,
    llm_router_compose,
    llm_router_enabled,
    resolve_llm_api_key,
)
from services.llm_guard import (
    extract_locked_values,
    finalize_compose_messages,
    locked_values_block,
    placeholder_map,
    reference_scripts_block,
)
from services.llm_router import GEO_META, _script_keys_for_geo, _scripts_delivered
from services.script_engine import (
    deposit_script_key,
    link_help_script_keys,
    registration_link_keys_for_geo,
    resolve_funnel_scripts,
)

logger = logging.getLogger(__name__)


@dataclass
class ComposeDecision:
    action: str
    messages: list[str]
    reference_script_keys: list[str]
    intent: str
    confidence: float
    note: str


def _compose_max_messages() -> int:
    try:
        return max(1, min(3, int(os.getenv("PAGER_LLM_COMPOSE_MAX_MSGS", "2") or "2")))
    except (TypeError, ValueError):
        return 2


async def _learn_dialog_block(geo: str, *, limit: int = 8) -> str:
    rows = await db.list_learn_success_examples(geo, limit=limit)
    if not rows:
        return ""
    lines = [
        "Successful operator↔client dialogs (match tone, pacing, empathy; "
        "do NOT copy links/codes/amounts verbatim — use LOCK placeholders):"
    ]
    for r in rows:
        dlg = str(r.get("dialog_text") or "").strip()
        folder = str(r.get("folder") or "").strip()
        if not dlg:
            continue
        excerpt = dlg[:1800].replace("\n", "\n  ")
        lines.append(f"--- folder={folder!r} ---\n  {excerpt}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _system_prompt(geo: str, learn_block: str, locked_block: str) -> str:
    meta = GEO_META.get(geo, GEO_META["zm"])
    return (
        "You write Messenger replies for a 1xBet acquisition funnel operator.\n"
        f"GEO: {geo} ({meta['label']}). Client language: {meta['language']}.\n"
        f"Funnel: {meta['funnel']}.\n"
        f"{learn_block}\n"
        f"{locked_block}\n"
        "Rules:\n"
        "- Write natural, motivating text like the successful dialogs above.\n"
        "- Reply with JSON only.\n"
        "- For promo codes, registration links, deposit amounts: use {{LOCK_N}} placeholders ONLY.\n"
        "- NEVER invent or alter URLs, promo codes, or money amounts.\n"
        "- If client declines / insults / scam accusation → action pause, messages=[].\n"
        "- If client asks for link/url/registration (even one word «Link») → "
        "reference_script_keys must include registration+link, NEVER game_id.\n"
        "- Usually 1 message; max 2 short messages in the array.\n"
        "- Do not repeat what the operator already sent (check scripts_delivered).\n"
        "- Zambia game ID always begins with 17 — never write 159, 59, or other prefixes.\n"
        "- Deposit OTP/validation/SMS codes: explain briefly in chat — do NOT ask for game ID.\n"
        'JSON: {"action":"send|pause|wait","messages":["..."],"reference_script_keys":["..."],'
        '"intent":"interested|positive|ready|question|unknown|declined|complaint|deposit_done",'
        '"confidence":0.0,"note":""}'
    )


def _hint_script_keys(
    geo: str,
    *,
    effective_step: int,
    rule_intent: str,
    text: str,
    outgoing_texts: list[str],
) -> list[str]:
    from services.ai_intent import is_requesting_registration_link

    if is_requesting_registration_link(text):
        return registration_link_keys_for_geo(geo, outgoing_texts)
    keys = resolve_funnel_scripts(
        effective_step,
        text,
        rule_intent,
        outgoing_texts=outgoing_texts,
        geo=geo,
    )
    if keys:
        return keys
    allowed = _script_keys_for_geo(geo)
    if effective_step <= 1:
        return [allowed[0]] if allowed else []
    return []


async def compose_client_reply(
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
    rescue: bool = False,
) -> ComposeDecision | None:
    if not llm_router_enabled():
        return None
    if not llm_router_compose() and not (llm_router_assist() and rescue):
        return None
    api_key = resolve_llm_api_key()
    if not api_key:
        return None

    g = (geo or "zm").strip().lower()
    if g not in GEO_META:
        g = "zm"

    hint_keys = _hint_script_keys(
        g,
        effective_step=effective_step,
        rule_intent=rule_intent,
        text=text,
        outgoing_texts=outgoing_texts,
    )
    if has_image and "link" in (text or "").lower():
        hint_keys = link_help_script_keys(g)

    locked_block = locked_values_block(g, script_keys=hint_keys or None)
    learn_block = await _learn_dialog_block(g)
    ref_block = ""
    if hint_keys:
        ref_block = reference_scripts_block(g, hint_keys[:4])

    user_payload: dict[str, Any] = {
        "geo": g,
        "client_text": (text or "").strip() or ("(photo)" if has_image else "(empty)"),
        "has_image": bool(has_image),
        "effective_step": int(effective_step or 0),
        "rule_intent": (rule_intent or "unknown").strip(),
        "folder": (folder or "").strip(),
        "reg_link_sent": bool(reg_link_sent),
        "deposit_script_sent": bool(deposit_script_sent),
        "deposit_key": deposit_script_key(g),
        "scripts_delivered": _scripts_delivered(outgoing_texts, g),
        "hint_script_keys": hint_keys,
        "rescue": bool(rescue),
    }
    if rescue:
        if llm_router_assist():
            user_payload["situation"] = (
                "Funnel rules found no script. The client asked a non-trivial question — "
                "answer it in operator tone using learned dialogs; keep momentum toward "
                "registration/deposit. Use LOCK placeholders for links/codes/amounts."
            )
        else:
            user_payload["situation"] = (
                "Rules/compose had no good reply — write a motivating operator message."
            )

    raw = await chat_completion_json(
        [
            {
                "role": "system",
                "content": _system_prompt(g, learn_block, locked_block)
                + ("\n" + ref_block if ref_block else ""),
            },
            {
                "role": "user",
                "content": "Compose the next operator reply:\n"
                + json.dumps(user_payload, ensure_ascii=False),
            },
        ],
        api_key=api_key,
        max_tokens=900,
        temperature=0.35,
    )
    if not raw:
        return None

    action = str(raw.get("action") or "wait").strip().lower()
    intent = str(raw.get("intent") or rule_intent or "unknown").strip().lower()
    note = str(raw.get("note") or "").strip()
    ref_keys = [
        str(k).strip()
        for k in (raw.get("reference_script_keys") or raw.get("script_keys") or [])
        if str(k).strip()
    ]
    if not ref_keys:
        ref_keys = list(hint_keys[:3])

    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    raw_messages = [
        str(m).strip() for m in (raw.get("messages") or []) if str(m).strip()
    ]
    raw_messages = raw_messages[: _compose_max_messages()]

    catalog_locked = extract_locked_values(g, script_keys=ref_keys or None)
    ph_map = placeholder_map(catalog_locked)
    messages = finalize_compose_messages(
        raw_messages,
        geo=g,
        script_keys=ref_keys,
        required_placeholders=ph_map,
    )

    if action == "send" and not messages:
        action = "wait"
    if action == "send" and confidence <= 0:
        confidence = 0.72 if messages else 0.0

    decision = ComposeDecision(
        action=action,
        messages=messages,
        reference_script_keys=ref_keys,
        intent=intent,
        confidence=max(0.0, min(1.0, confidence)),
        note=note,
    )
    logger.info(
        "LLM compose geo=%s action=%s msgs=%s refs=%s conf=%.2f intent=%s",
        g,
        decision.action,
        len(decision.messages),
        decision.reference_script_keys[:3],
        decision.confidence,
        decision.intent,
    )
    return decision
