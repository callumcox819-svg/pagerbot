"""Load ZM scripts and infer step from message history."""

from __future__ import annotations

import re
from pathlib import Path

from config import SCRIPTS_DIR

# Logical send order (not folder order in Pager UI)
SCRIPT_KEYS = [
    "01_intro",
    "02_how_it_works",
    "03_zmw_table",
    "04_registration",
    "05_link",
    "10_reg_screenshot",
    "07_game_id",
    "06_deposit",
    "08_tg_invite",
    "09_tg_link",
]

_cache: dict[str, str] = {}


def load_script(geo: str, key: str) -> str:
    cache_key = f"{geo}/{key}"
    if cache_key in _cache:
        return _cache[cache_key]
    path = SCRIPTS_DIR / geo / f"{key}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"Script missing: {path}")
    text = path.read_text(encoding="utf-8").strip()
    _cache[cache_key] = text
    return text


def script_sent_in_history(outgoing_texts: list[str], snippet: str) -> bool:
    sn = snippet.strip().lower()
    if not sn:
        return False
    for t in outgoing_texts:
        if sn in t.lower() or t.lower()[:80] in sn:
            return True
    return False


def infer_step_from_history(
    messages: list[dict], operator_id: str = ""
) -> int:
    """0=new … 9=TG link sent, 10=subscriber games phase."""
    uid = (operator_id or "").strip()
    outgoing: list[str] = []
    for m in messages:
        if (m.get("messageDirection") or "").lower() not in ("outgoing", "out"):
            continue
        if not m.get("text"):
            continue
        author = str(m.get("authorId") or "").strip()
        if uid and author != uid:
            continue
        if not uid and not author:
            continue
        outgoing.append(m.get("text") or "")
    outgoing_joined = "\n".join(outgoing).lower()

    if "t.me/+" in outgoing_joined or "vhfjiofy" in outgoing_joined:
        return 9
    if "join our private telegram" in outgoing_joined:
        return 8
    if "deposit" in outgoing_joined and "screenshot" in outgoing_joined:
        return 7
    if "game id" in outgoing_joined or "begins with 16" in outgoing_joined:
        return 6
    if "ibb.co" in outgoing_joined or "скрин реги" in outgoing_joined:
        return 5
    if "tinyurl.com/zam577" in outgoing_joined:
        return 4
    if "promo code zam577" in outgoing_joined or "registration link" in outgoing_joined:
        return 3
    if "30 zmw - 300 zmw" in outgoing_joined or "are you ready to start" in outgoing_joined:
        return 2
    if "how it works" in outgoing_joined:
        return 2
    if "analytical systems" in outgoing_joined or "casino platforms" in outgoing_joined:
        return 1
    return 0


def scripts_to_resend_for_step(hist_step: int) -> list[str]:
    """Resend scripts when a prior attempt was marked processed but never delivered."""
    if hist_step < 1:
        return ["01_intro"]
    if hist_step < 2:
        return ["02_how_it_works", "03_zmw_table"]
    if hist_step < 4:
        return ["04_registration", "05_link"]
    if hist_step < 5:
        return ["10_reg_screenshot"]
    if hist_step < 6:
        return ["07_game_id"]
    if hist_step < 7:
        return ["06_deposit"]
    if hist_step < 9:
        return ["08_tg_invite", "09_tg_link"]
    return []


def scripts_to_send_after_intent(step: int, intent: str, geo: str = "zm") -> list[str]:
    """Return script keys to POST in order."""
    if intent == "interested" and step < 1:
        return ["01_intro"]
    if intent == "positive" and step < 2:
        return ["02_how_it_works", "03_zmw_table"]
    if intent in ("positive", "ready") and 2 <= step < 4:
        return ["04_registration", "05_link"]
    if step == 4:
        return ["10_reg_screenshot"]
    if intent == "game_id_text" or (intent == "image_only" and step >= 5):
        if step < 7:
            return ["07_game_id"] if step < 6 else []
    if step >= 8 and intent != "joined":
        return ["08_tg_invite", "09_tg_link"]
    return []


def extract_game_id(text: str) -> str:
    m = re.search(r"\b(16\d{6,})\b", text or "")
    if m:
        return m.group(1)
    m = re.search(r"ACCOUNT\s*(\d+)", text or "", re.I)
    if m:
        return m.group(1)
    return ""
