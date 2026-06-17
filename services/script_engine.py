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

# Never auto-send to clients (Russian internal note / manual-only in Pager).
AUTO_SKIP_SCRIPT_KEYS: frozenset[str] = frozenset({"10_reg_screenshot"})

SAVED_REPLY_FOLDER_NAMES = ("Замбія", "Замбия", "Zambia", "Замб")

# Egypt — folder name in Pager «Збережені відповіді» (create manually).
EG_SAVED_REPLY_FOLDER_NAMES = ("hapkatest", "Hapkatest", "HAPKATEST")

EG_SCRIPT_UI_SNIPPETS: dict[str, str] = {
    "01_intro": "إنت من مصر",
    "02_how_it_works": "تمام كده",
    "04_registration": "هبعتلك اللينك دلوقتي",
    "05_link": "tinyurl.com/Egypt0011",
    "06_deposit": "+ الأخضر",
    "07_game_id": "يبدأ ب 17",
    "08_app_or_browser": "ينفع الاتنين",
}

# When needle is a substring of another template, exclude those bodies.
EG_SCRIPT_EXCLUDE_SNIPPETS: dict[str, tuple[str, ...]] = {
    "04_registration": ("تمام كده", "هتعمل إيداع"),
}

_cache: dict[str, str] = {}


def saved_reply_folder_names(geo: str = "zm") -> tuple[str, ...]:
    if geo == "eg":
        return EG_SAVED_REPLY_FOLDER_NAMES
    return SAVED_REPLY_FOLDER_NAMES


def script_exclude_snippets(key: str, geo: str = "zm") -> tuple[str, ...]:
    if geo == "eg":
        return EG_SCRIPT_EXCLUDE_SNIPPETS.get(key, ())
    return ()


def script_ui_snippet(key: str, geo: str = "zm") -> str:
    """Text needle for locating a saved reply in Pager UI."""
    snippets = EG_SCRIPT_UI_SNIPPETS if geo == "eg" else SCRIPT_UI_SNIPPETS
    sn = snippets.get(key, "").strip()
    if sn:
        return sn
    try:
        return load_script(geo, key)[:48].strip()
    except FileNotFoundError:
        return key


def script_verify_snippet(key: str, geo: str = "zm") -> str:
    """Substring used to verify delivery in message history."""
    snippets = EG_SCRIPT_UI_SNIPPETS if geo == "eg" else SCRIPT_UI_SNIPPETS
    sn = snippets.get(key, "").strip()
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


def _step_for_outgoing_text_eg(text: str) -> int:
    t = (text or "").lower()
    if "يبدأ ب 17" in t or "يبدا ب 17" in t:
        return 7
    if "+ الأخضر" in t or "الأخضر" in t and "إيداع" in t:
        return 6
    if "ينفع الاتنين" in t:
        return 5
    if "tinyurl.com/egypt0011" in t:
        return 4
    if "هبعتلك اللينك" in t or "انسخه وحطه في chrome" in t:
        return 4
    if "eg011" in t and ("كروم" in t or "chrome" in t or "مصر" in t):
        return 4
    if "تمام كده" in t:
        return 2
    if "إنت من مصر" in t or "انت من مصر" in t:
        return 1
    return 0


def _step_for_outgoing_text(text: str, geo: str = "zm") -> int:
    """Map one operator message to funnel step (strict markers only)."""
    if geo == "eg":
        return _step_for_outgoing_text_eg(text)
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
        return 3
    if "here's what you can get" in t or "here is what you can get" in t:
        return 3
    if "are you ready to start today" in t:
        return 3
    if re.search(r"how it works:\s*\n\s*1\)", t):
        return 2
    if "hi! i want to show you" in t or "analytical systems" in t:
        return 1
    return 0


def infer_step_from_history(
    messages: list[dict], operator_id: str = "", *, geo: str = "zm"
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
        step = max(step, _step_for_outgoing_text(text, geo))
    return step


def infer_step_from_thread(messages: list[dict], *, geo: str = "zm") -> int:
    """Funnel step from any delivered team reply (manual operator sends count too)."""
    step = 0
    for m in messages:
        if (m.get("messageDirection") or "").lower() not in ("outgoing", "out"):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if not (m.get("isDelivered") or m.get("facebookMessageId")):
            continue
        step = max(step, _step_for_outgoing_text(text, geo))
    return step


def should_send_deposit_script(
    text: str,
    step: int,
    outgoing_texts: list[str],
    *,
    folder_step: int = 0,
    geo: str = "zm",
) -> bool:
    """Client confirmed reg / on 1xbet — send 06_deposit once link was sent."""
    from services.ai_intent import (
        is_deferral_reply,
        is_registration_confirmed,
        is_registration_pending,
    )

    if is_deferral_reply(text) or is_registration_pending(text):
        return False
    if not is_registration_confirmed(text):
        return False
    link_sent = script_sent_in_history(
        outgoing_texts, script_ui_snippet("05_link", geo)
    )
    min_step = 4 if geo == "eg" else 4
    if not link_sent and max(step, folder_step) < min_step:
        return False
    if script_sent_in_history(
        outgoing_texts, script_ui_snippet("06_deposit", geo)
    ):
        return False
    return True


def scripts_for_registration_resend(hist_step: int) -> list[str]:
    """Client has not registered yet — resend registration + link."""
    if hist_step < 1:
        return ["01_intro"]
    if hist_step < 3:
        return ["02_how_it_works", "03_zmw_table"]
    if hist_step < 5:
        return ["04_registration", "05_link"]
    return []


def scripts_for_positive_reply(hist_step: int) -> list[str]:
    """After intro: explain (02+03), then registration (04+05) before link sent."""
    if hist_step < 1:
        return ["01_intro"]
    if hist_step < 3:
        return ["02_how_it_works", "03_zmw_table"]
    if hist_step < 4:
        return ["04_registration", "05_link"]
    return []


def filter_auto_script_keys(keys: list[str]) -> list[str]:
    return [k for k in keys if k not in AUTO_SKIP_SCRIPT_KEYS]


def scripts_to_resend_for_step(hist_step: int) -> list[str]:
    """Resend scripts when a prior attempt was marked processed but never delivered."""
    if hist_step < 1:
        return ["01_intro"]
    if hist_step < 3:
        return ["02_how_it_works", "03_zmw_table"]
    if hist_step < 5:
        return ["04_registration", "05_link"]
    if hist_step < 7:
        return ["06_deposit"]
    if hist_step < 8:
        return ["07_game_id"]
    if hist_step < 9:
        return ["08_tg_invite", "09_tg_link"]
    return []


def resolve_funnel_scripts(
    effective_step: int,
    text: str,
    intent: str,
    *,
    outgoing_texts: list[str] | None = None,
    geo: str = "zm",
) -> list[str]:
    """Pick next Pager saved-reply keys from funnel step + client message."""
    from services.ai_intent import (
        is_app_or_browser_question,
        is_deferral_reply,
        is_funnel_positive_reaction,
        is_ready_for_registration,
        is_registration_confirmed,
        is_registration_pending,
        wants_details_after_intro,
        wants_registration_link,
    )

    out = outgoing_texts or []
    t = (text or "").strip()

    if is_deferral_reply(t):
        return []

    if effective_step < 1:
        return ["01_intro"]

    if geo == "eg":
        how_sent = script_sent_in_history(
            out, script_ui_snippet("02_how_it_works", geo)
        )
        link_sent = script_sent_in_history(
            out, script_ui_snippet("05_link", geo)
        )

        def _eg_reg_scripts() -> list[str]:
            if link_sent:
                return []
            return ["04_registration", "05_link"]

        # After «как работает» — only reg instructions + link, never 02 again.
        if how_sent and not link_sent:
            if (
                wants_registration_link(t)
                or is_ready_for_registration(t)
                or is_registration_pending(t)
                or is_funnel_positive_reaction(
                    t, funnel_step=effective_step
                )
                or intent
                in ("ready", "positive", "question", "interested")
                or effective_step >= 2
            ):
                return _eg_reg_scripts()

        if (
            is_app_or_browser_question(t)
            and 2 <= effective_step < 6
            and not script_sent_in_history(
                out, script_ui_snippet("08_app_or_browser", geo)
            )
        ):
            return ["08_app_or_browser"]
        if effective_step < 2:
            if how_sent:
                if (
                    is_ready_for_registration(t)
                    or wants_registration_link(t)
                    or is_funnel_positive_reaction(
                        t, funnel_step=effective_step
                    )
                    or intent in ("ready", "positive", "question")
                ):
                    return _eg_reg_scripts()
                return []
            if (
                intent in ("interested", "positive", "ready", "question")
                or wants_details_after_intro(t)
                or is_ready_for_registration(t)
                or is_funnel_positive_reaction(
                    t, funnel_step=effective_step
                )
            ):
                return ["02_how_it_works"]
            return []
        if effective_step < 4:
            if (
                is_ready_for_registration(t)
                or wants_registration_link(t)
                or intent in ("ready", "positive", "question")
                or is_funnel_positive_reaction(t, funnel_step=effective_step)
            ):
                return _eg_reg_scripts()
            return []
        if is_registration_pending(t):
            return _eg_reg_scripts()
        if effective_step < 7:
            if intent == "game_id_text":
                return []
            if is_registration_confirmed(t) or intent == "joined":
                if should_send_deposit_script(
                    t, effective_step, out, folder_step=0, geo=geo
                ):
                    return ["06_deposit"]
            return []
        if effective_step < 8 and intent == "game_id_text":
            return ["07_game_id"]
        return []

    if effective_step < 3:
        if intent in ("interested", "ready") or is_ready_for_registration(t):
            return ["02_how_it_works", "03_zmw_table"]
        return []

    if effective_step < 4:
        if is_ready_for_registration(t) or intent == "ready":
            return ["04_registration", "05_link"]
        return []

    # Link already sent (step 4+)
    if is_registration_pending(t):
        return ["04_registration", "05_link"]

    if effective_step < 7:
        if intent == "game_id_text":
            return []
        if is_registration_confirmed(t) or intent == "joined":
            if should_send_deposit_script(
                t, effective_step, out, folder_step=0
            ):
                return ["06_deposit"]
        return []

    if effective_step < 8 and intent == "game_id_text":
        return ["07_game_id"]

    return []


def scripts_to_send_after_intent(step: int, intent: str, geo: str = "zm") -> list[str]:
    """Return script keys to POST in order."""
    if intent == "interested" and step < 1:
        return ["01_intro"]
    if intent in ("interested", "positive", "ready") and 1 <= step < 3:
        return ["02_how_it_works", "03_zmw_table"]
    if intent in ("interested", "positive", "ready") and 3 <= step < 4:
        return ["04_registration", "05_link"]
    if intent in ("deposit_done",) or (
        intent == "image_only" and step >= 4
    ):
        if step < 6:
            return ["07_game_id"]
    if intent == "game_id_text" or (intent == "image_only" and step >= 5):
        if step < 7 and step >= 4:
            return ["07_game_id"] if step < 6 else []
    if step >= 8 and intent != "joined":
        return ["08_tg_invite", "09_tg_link"]
    return []


def extract_game_id(text: str, geo: str = "zm") -> str:
    if geo == "eg":
        m = re.search(r"\b(17\d{6,})\b", text or "")
        if m:
            return m.group(1)
    m = re.search(r"\b(16\d{6,})\b", text or "")
    if m:
        return m.group(1)
    m = re.search(r"ACCOUNT\s*(\d+)", text or "", re.I)
    if m:
        return m.group(1)
    return ""
