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

# Unique substrings to find the right row in Pager saved-replies sidebar (Замбія).
SCRIPT_UI_SNIPPETS: dict[str, str] = {
    "01_intro": "Hi! I want to show you",
    "02_how_it_works": "How it works:",
    "03_zmw_table": "30 ZMW - 300 ZMW",
    "04_registration": "promo code ZAM577",
    "05_link": "tinyurl.com/ZAM577",
    "06_deposit": "click \"Deposit\"",
    "07_game_id": "begins with 16",
    "08_tg_invite": "private Telegram channel",
    "09_tg_link": "t.me/+",
    "10_reg_screenshot": "ibb.co",
}

SAVED_REPLY_FOLDER_NAMES = ("Замбія", "Замбия", "Zambia", "Замб")

_cache: dict[str, str] = {}


def script_ui_snippet(key: str) -> str:
    """Text needle for locating a saved reply in Pager UI."""
    sn = SCRIPT_UI_SNIPPETS.get(key, "").strip()
    if sn:
        return sn
    try:
        return load_script("zm", key)[:48].strip()
    except FileNotFoundError:
        return key


def script_verify_snippet(key: str, geo: str = "zm") -> str:
    """Substring used to verify delivery in message history."""
    sn = SCRIPT_UI_SNIPPETS.get(key, "").strip()
    if sn:
        return sn
    return load_script(geo, key)[:80].strip()


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


def _step_for_outgoing_text(text: str) -> int:
    """Map one operator message to funnel step (strict markers only)."""
    t = (text or "").lower()
    if "t.me/+" in t or "vhfjiofy" in t:
        return 9
    if "join our private telegram" in t:
        return 8
    if 'click "deposit"' in t or "minimum deposit amount" in t:
        return 7
    if "begins with 16" in t or "send me your game id" in t:
        return 6
    if "ibb.co" in t or "скрин реги" in t:
        return 5
    if "tinyurl.com/zam577" in t:
        return 4
    if "promo code zam577" in t:
        return 3
    if "30 zmw - 300 zmw" in t:
        return 2
    if "are you ready to start today" in t:
        return 2
    if re.search(r"how it works:\s*\n\s*1\)", t):
        return 2
    if "hi! i want to show you" in t or "analytical systems" in t:
        return 1
    return 0


def infer_step_from_history(
    messages: list[dict], operator_id: str = ""
) -> int:
    """0=new … 9=TG link sent. Uses chronological operator messages (max step)."""
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
        if not (m.get("isDelivered") or m.get("facebookMessageId")):
            continue
        outgoing.append(m.get("text") or "")

    step = 0
    for text in outgoing:
        step = max(step, _step_for_outgoing_text(text))
    return step


def scripts_for_positive_reply(hist_step: int) -> list[str]:
    """After client says Yes/OK — next scripts in funnel order."""
    if hist_step < 1:
        return ["01_intro"]
    if hist_step < 2:
        return ["02_how_it_works", "03_zmw_table"]
    if hist_step < 4:
        return ["04_registration", "05_link"]
    if hist_step < 5:
        return ["10_reg_screenshot"]
    return scripts_to_resend_for_step(hist_step)


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
