"""Load ZM scripts and infer step from message history."""

from __future__ import annotations

import os
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
    "09_tg_invite": "الاستراتيجيات والتكتيكات",
    "10_tg_link": "t7iYS46b2Ls2YWRk",
    "08_tg_invite": "الاستراتيجيات والتكتيكات",
    "09_tg_link": "t7iYS46b2Ls2YWRk",
}

_EG_KEY_ALIASES: dict[str, str] = {
    "08_tg_invite": "09_tg_invite",
    "09_tg_link": "10_tg_link",
}


def resolve_eg_key(key: str) -> str:
    return _EG_KEY_ALIASES.get(key, key)

# Djibouti — French saved replies folder in Pager (create manually).
DJ_SAVED_REPLY_FOLDER_NAMES = ("Djibouti", "DJIBOUTI", "Djibouti FR", "DJ")

DJ_SCRIPT_UI_SNIPPETS: dict[str, str] = {
    "01_intro": "Moses Zulu",
    "02_how_it_works": "Voici le plan",
    "03_zmw_table": "3 000 DJF",
    "04_registration": "BJI777",
    "05_link": "tinyurl.com/Djibouti7",
    "06_deposit": "bouton vert",
    "07_game_id": "commence par 17",
    "08_tg_invite": "canal Telegram privé",
    "09_tg_link": "eylDdIKyykg0MWUyc",
    "10_reg_screenshot": "Problème à l'inscription",
}

# Cameroon — folder «Камерун» in Pager saved replies (French templates).
CM_SAVED_REPLY_FOLDER_NAMES = (
    "Камерун",
    "камерун",
    "Cameroon",
    "CAMEROON",
    "Cameroun",
    "CM",
)

CM_SCRIPT_UI_SNIPPETS: dict[str, str] = {
    "01_intro": "Tu es du Cameroun",
    "01_intro_2": "Mon équipe cumule",
    "02_age": "Quel âge avez-vous",
    "03_steps": "voici comment ça fonctionne",
    "04_tier": "140 000 CFA",
    "05_registration": "CASH056",
    "06_link": "Camerun01",
    "07_chrome": "Google Chrome",
    "08_game_id": "commence par 17",
    "09_deposit": "bouton vert",
    "10_tg_invite": "canal Telegram privé",
    "11_tg_link": "XtIY04zvcVw2YzZi",
    # ZM-style aliases (worker / shared helpers)
    "02_how_it_works": "Quel âge avez-vous",
    "03_zmw_table": "140 000 CFA",
    "04_registration": "CASH056",
    "05_link": "Camerun01",
    "06_deposit": "bouton vert",
    "07_game_id": "commence par 17",
    "08_tg_invite": "canal Telegram privé",
    "09_tg_link": "XtIY04zvcVw2YzZi",
}

CM_REG_SCRIPT_KEYS = frozenset({"05_registration", "06_link", "07_chrome"})

_CM_KEY_ALIASES: dict[str, str] = {
    "02_how_it_works": "02_age",
    "03_zmw_table": "04_tier",
    "04_registration": "05_registration",
    "05_link": "06_link",
    "06_deposit": "09_deposit",
    "07_game_id": "08_game_id",
    "08_tg_invite": "10_tg_invite",
    "09_tg_link": "11_tg_link",
}


def resolve_cm_key(key: str) -> str:
    return _CM_KEY_ALIASES.get(key, key)


def deposit_script_key(geo: str = "zm") -> str:
    return "09_deposit" if geo == "cm" else "06_deposit"


def game_id_script_key(geo: str = "zm") -> str:
    return "08_game_id" if geo == "cm" else "07_game_id"


def reg_script_keys_set(geo: str = "zm") -> frozenset[str]:
    if geo == "cm":
        return CM_REG_SCRIPT_KEYS
    return frozenset({"04_registration", "05_link"})

GEO_SCRIPT_UI_SNIPPETS: dict[str, dict[str, str]] = {
    "zm": SCRIPT_UI_SNIPPETS,
    "eg": EG_SCRIPT_UI_SNIPPETS,
    "dj": DJ_SCRIPT_UI_SNIPPETS,
    "cm": CM_SCRIPT_UI_SNIPPETS,
}

# When needle is a substring of another template, exclude those bodies.
EG_SCRIPT_EXCLUDE_SNIPPETS: dict[str, tuple[str, ...]] = {
    "04_registration": ("تمام كده", "هتعمل إيداع"),
}

_cache: dict[str, str] = {}


def saved_reply_folder_names(geo: str = "zm") -> tuple[str, ...]:
    if geo == "eg":
        return EG_SAVED_REPLY_FOLDER_NAMES
    if geo == "dj":
        return DJ_SAVED_REPLY_FOLDER_NAMES
    if geo == "cm":
        return CM_SAVED_REPLY_FOLDER_NAMES
    return SAVED_REPLY_FOLDER_NAMES


def script_exclude_snippets(key: str, geo: str = "zm") -> tuple[str, ...]:
    if geo == "eg":
        return EG_SCRIPT_EXCLUDE_SNIPPETS.get(key, ())
    return ()


def script_ui_snippet(key: str, geo: str = "zm") -> str:
    """Text needle for locating a saved reply in Pager UI."""
    if geo == "cm":
        key = resolve_cm_key(key)
    elif geo == "eg":
        key = resolve_eg_key(key)
    snippets = GEO_SCRIPT_UI_SNIPPETS.get(geo) or SCRIPT_UI_SNIPPETS
    sn = snippets.get(key, "").strip()
    if sn:
        return sn
    try:
        return load_script(geo, key)[:48].strip()
    except FileNotFoundError:
        return key


def script_verify_snippet(key: str, geo: str = "zm") -> str:
    """Substring used to verify delivery in message history."""
    if geo == "cm":
        key = resolve_cm_key(key)
    elif geo == "eg":
        key = resolve_eg_key(key)
    snippets = GEO_SCRIPT_UI_SNIPPETS.get(geo) or SCRIPT_UI_SNIPPETS
    sn = snippets.get(key, "").strip()
    if sn:
        return sn
    return load_script(geo, key)[:80].strip()


def load_script(geo: str, key: str) -> str:
    if geo == "cm":
        key = resolve_cm_key(key)
    elif geo == "eg":
        key = resolve_eg_key(key)
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
    if "أهلاً" in text or "اهلا" in t:
        if "مصر" in t or "دخل" in t or "كازينو" in t:
            return 1
    if "حابب تفاصيل" in t or "تفاصيل" in t and "قولي" in t:
        return 1
    if "t7iys46b2ls2ywrd" in t or "t.me/+" in t:
        return 9
    if "الاستراتيجيات والتكتيكات" in text or "استراتيجيات" in t:
        return 8
    return 0


def _step_for_outgoing_text_cm(text: str) -> int:
    t = (text or "").lower()
    if "xtiy04zvcvw" in t or "t.me/+" in t:
        return 9
    if "canal telegram" in t and (
        "privé" in t or "prive" in t or "stratégies" in t or "strategies" in t
    ):
        return 8
    if "bouton vert" in t or ("déposer" in t and "mtn" in t):
        return 7
    if "commence par 17" in t or "numéro de joueur" in t:
        return 6
    if "camerun01" in t:
        return 5
    if "google chrome" in t and "colle" in t:
        return 5
    if "cash056" in t:
        return 4
    if "140 000 cfa" in t or "190 000 cfa" in t:
        return 4
    if "1 000 francs" in t or "dépôt minimum de 1 000" in t:
        return 3
    if "quel âge" in t or "age avez-vous" in t or "age as-tu" in t:
        return 2
    if "business comme un autre" in t or "gagner ensemble" in t or "mon équipe cumule" in t:
        return 2
    if "cameroun" in t or "tu es du cameroun" in t:
        return 1
    return 0


def _step_for_outgoing_text_dj(text: str) -> int:
    t = (text or "").lower()
    if "eylddikyykg" in t or "5fwd_blxe" in t or "t.me/+" in t:
        return 9
    if "canal telegram" in t or "rejoins notre" in t:
        return 8
    if "déposer" in t or "deposer" in t or "bouton vert" in t:
        return 7
    if "commence par 17" in t or "identifiant de jeu" in t:
        return 6
    if "tinyurl.com/djibouti7" in t:
        return 5
    if "bji777" in t and "voici le lien" in t:
        return 5
    if "bji777" in t or "coupe le vpn" in t:
        return 4
    if "3 000 djf" in t or "8 000 djf" in t or "15 000 djf" in t:
        return 3
    if "voici le plan" in t or "étape par étape" in t:
        return 2
    if "moses zulu" in t:
        return 1
    return 0


def _step_for_outgoing_text(text: str, geo: str = "zm") -> int:
    """Map one operator message to funnel step (strict markers only)."""
    if geo == "eg":
        return _step_for_outgoing_text_eg(text)
    if geo == "dj":
        return _step_for_outgoing_text_dj(text)
    if geo == "cm":
        return _step_for_outgoing_text_cm(text)
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


def reg_link_sent_in_history(
    outgoing_texts: list[str], *, geo: str = "zm"
) -> bool:
    """True if registration link (or promo) already sent in this thread."""
    out = outgoing_texts or []
    if script_sent_in_history(out, script_ui_snippet("05_link", geo)):
        return True
    blob = "\n".join(out).lower()
    if geo == "dj":
        return "tinyurl.com/djibouti7" in blob or "bji777" in blob
    if geo == "cm":
        if script_sent_in_history(out, script_ui_snippet("06_link", geo)):
            return True
        return "camerun01" in blob or "cash056" in blob
    if geo == "eg":
        return "tinyurl.com/egypt" in blob or "egypt0011" in blob
    return "tinyurl.com/zam577" in blob or "zam577" in blob


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
    link_sent = reg_link_sent_in_history(outgoing_texts, geo=geo)
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


CM_REG_SEND_KEYS = frozenset({"05_registration", "06_link", "07_chrome"})
CM_INTRO_SEND_KEYS = frozenset({"01_intro", "01_intro_2"})
ZM_REG_SEND_KEYS = frozenset({"04_registration", "05_link"})
ZM_EXPLAIN_SEND_KEYS = frozenset({"02_how_it_works", "03_zmw_table"})


def bodies_for_script_keys(geo: str, keys: list[str]) -> list[str]:
    """One script key → one outbound message (text and link never merged)."""
    keys = filter_auto_script_keys(list(keys or []))
    if not keys:
        return []
    keyset = set(keys)
    if geo == "cm" and CM_INTRO_SEND_KEYS.issubset(keyset):
        return [load_script(geo, k) for k in ("01_intro", "01_intro_2")]
    if geo == "cm" and CM_REG_SEND_KEYS.issubset(keyset):
        return [load_script(geo, k) for k in ("05_registration", "07_chrome", "06_link")]
    if geo in ("zm", "dj") and ZM_EXPLAIN_SEND_KEYS.issubset(keyset):
        return [load_script(geo, k) for k in ("02_how_it_works", "03_zmw_table")]
    if geo in ("zm", "dj") and ZM_REG_SEND_KEYS.issubset(keyset):
        return [load_script(geo, k) for k in ("04_registration", "05_link")]
    return [load_script(geo, k) for k in keys]


def uses_combined_script_bundle(geo: str, keys: list[str]) -> bool:
    """True when several script keys collapse into fewer outbound messages."""
    keys = filter_auto_script_keys(list(keys or []))
    if not keys:
        return False
    return len(bodies_for_script_keys(geo, keys)) < len(keys)


def uses_combined_reg_bundle(geo: str, keys: list[str]) -> bool:
    keys = filter_auto_script_keys(list(keys or []))
    if geo == "cm" and CM_REG_SEND_KEYS.issubset(set(keys)):
        return True
    if geo in ("zm", "dj") and ZM_REG_SEND_KEYS.issubset(set(keys)):
        return True
    return False


def browser_first_geos() -> frozenset[str]:
    raw = (os.getenv("PAGER_BROWSER_FIRST_GEOS") or "cm,dj").strip().lower()
    return frozenset(g.strip() for g in raw.split(",") if g.strip())


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
    attachments: list | None = None,
    geo: str = "zm",
    message_reaction: str | None = None,
) -> list[str]:
    """Pick next Pager saved-reply keys from funnel step + client message."""
    from services.ai_intent import (
        _AR_INTERESTED,
        _AR_POSITIVE,
        is_app_or_browser_question,
        is_deferral_reply,
        is_funnel_positive_reaction,
        is_refusal_reply,
        is_registration_confirmed,
        is_registration_pending,
        is_ready_for_registration,
        is_registration_confirmed,
        is_registration_pending,
        wants_details_after_intro,
        wants_registration_link,
        is_post_link_registration_question,
        is_what_required_question,
    )

    out = outgoing_texts or []
    t = (text or "").strip()

    def _positive_signal() -> bool:
        return is_funnel_positive_reaction(
            t,
            attachments,
            funnel_step=effective_step,
            geo=geo,
            message_reaction=message_reaction,
        )

    if is_deferral_reply(t) or is_refusal_reply(t) or intent == "declined":
        return []

    if geo == "cm":
        from services.ai_intent import is_age_answer, is_deposit_tier_choice

        intro_sn = script_ui_snippet("01_intro", geo)
        intro2_sn = script_ui_snippet("01_intro_2", geo)
        age_sn = script_ui_snippet("02_age", geo)
        steps_sn = script_ui_snippet("03_steps", geo)
        tier_sn = script_ui_snippet("04_tier", geo)
        dep_key = deposit_script_key(geo)
        gid_key = game_id_script_key(geo)
        intro_sent = script_sent_in_history(out, intro_sn)
        intro2_sent = script_sent_in_history(out, intro2_sn)
        age_sent = script_sent_in_history(out, age_sn)
        steps_sent = script_sent_in_history(out, steps_sn)
        tier_sent = script_sent_in_history(out, tier_sn)
        link_sent = reg_link_sent_in_history(out, geo=geo)

        if tier_sent and is_deposit_tier_choice(t, geo=geo) and not link_sent:
            return ["05_registration", "06_link", "07_chrome"]

        if wants_registration_link(t) and not link_sent and steps_sent:
            return ["05_registration", "06_link", "07_chrome"]

        if effective_step < 1:
            if not intro_sent:
                if intent in ("interested", "positive", "ready", "question") or _positive_signal():
                    return ["01_intro", "01_intro_2"]
                return []
            if intro_sent and not intro2_sent:
                return ["01_intro_2"]
            return []

        if effective_step < 2:
            if intro2_sent and not age_sent:
                if (
                    intent in ("interested", "positive", "ready", "question")
                    or _positive_signal()
                    or wants_details_after_intro(t)
                ):
                    return ["02_age"]
            return []

        if effective_step < 3:
            if age_sent and not steps_sent:
                if (
                    is_age_answer(t)
                    or intent in ("positive", "ready", "interested", "question")
                    or _positive_signal()
                ):
                    return ["03_steps"]
            return []

        if effective_step < 4:
            if wants_registration_link(t) and steps_sent and not link_sent:
                return ["05_registration", "06_link", "07_chrome"]
            if steps_sent and not tier_sent:
                if (
                    intent in ("positive", "ready", "interested", "question")
                    or _positive_signal()
                    or is_ready_for_registration(t, geo=geo)
                ):
                    return ["04_tier"]
            if tier_sent and (
                is_deposit_tier_choice(t, geo=geo) or _positive_signal()
            ):
                if link_sent:
                    return []
                return ["05_registration", "06_link", "07_chrome"]
            return []

        if is_registration_confirmed(t) and link_sent:
            if should_send_deposit_script(
                t, effective_step, out, folder_step=0, geo=geo
            ):
                return [dep_key]
            return []

        if is_registration_pending(t) and not link_sent:
            return ["05_registration", "06_link", "07_chrome"]
        if is_registration_pending(t) and link_sent:
            return []

        if effective_step < 7:
            if intent == "game_id_text":
                return []
            dep_sn = script_ui_snippet(dep_key, geo)
            if (
                link_sent
                and effective_step >= 4
                and not is_deposit_tier_choice(t, geo=geo)
                and not script_sent_in_history(out, dep_sn)
                and (
                    is_what_required_question(t)
                    or is_post_link_registration_question(t)
                    or intent in ("question", "positive", "interested", "ready")
                )
            ):
                return [dep_key]
            if is_registration_confirmed(t) or intent == "joined":
                if should_send_deposit_script(
                    t, effective_step, out, folder_step=0, geo=geo
                ):
                    return [dep_key]
            if (
                tier_sent
                and not link_sent
                and (
                    is_ready_for_registration(t, geo=geo)
                    or wants_registration_link(t)
                    or intent in ("ready", "interested", "positive", "question")
                    or _positive_signal()
                )
            ):
                return ["05_registration", "06_link", "07_chrome"]
            return []

        if effective_step < 8 and intent == "game_id_text":
            return [gid_key]

        return []

    if effective_step < 1:
        intro_sn = script_ui_snippet("01_intro", geo)
        if script_sent_in_history(out, intro_sn):
            if intent in ("positive", "ready", "interested", "question") or _positive_signal():
                return ["02_how_it_works", "03_zmw_table"]
            return []
        if intent in ("interested", "positive", "ready", "question"):
            return ["01_intro"]
        if _positive_signal():
            return ["01_intro"]
        return []

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
                or is_ready_for_registration(t, geo=geo)
                or is_registration_pending(t)
                or is_funnel_positive_reaction(
                    t, attachments, funnel_step=effective_step
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
                    is_ready_for_registration(t, geo=geo)
                    or wants_registration_link(t)
                    or is_funnel_positive_reaction(
                        t, attachments, funnel_step=effective_step
                    )
                    or intent in ("ready", "positive", "question", "interested")
                    or re.search(r"استثمر|أريد|اريد|ايو|نجرب|مهتم|تمام", t, re.I)
                ):
                    return _eg_reg_scripts()
                return []
            if (
                intent in ("interested", "positive", "ready", "question")
                or wants_details_after_intro(t)
                or is_ready_for_registration(t, geo=geo)
                or is_funnel_positive_reaction(
                    t, attachments, funnel_step=effective_step
                )
                or re.search(
                    r"استثمر|أريد أن|اريد ان|أنا مهتم|موضوع|شغل|ازاي|إزاي",
                    t,
                    re.I,
                )
            ):
                return ["02_how_it_works"]
            return []
        if effective_step < 4:
            if (
                is_ready_for_registration(t, geo=geo)
                or wants_registration_link(t)
                or intent in ("ready", "positive", "question")
                or is_funnel_positive_reaction(t, attachments, funnel_step=effective_step)
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
            dep_sn = script_ui_snippet("06_deposit", geo)
            if link_sent and effective_step >= 4 and not script_sent_in_history(
                out, dep_sn
            ) and (
                is_what_required_question(t)
                or is_post_link_registration_question(t)
                or intent in ("question", "positive", "interested", "ready")
            ):
                return ["06_deposit"]
            if effective_step >= 4 and not link_sent and (
                intent in ("interested", "positive", "ready", "question")
                or _positive_signal()
                or _AR_INTERESTED.search(t)
                or _AR_POSITIVE.search(t)
            ):
                return _eg_reg_scripts()
            return []
        if effective_step < 8 and intent == "game_id_text":
            return ["07_game_id"]
        if effective_step < 9:
            tg_sn = script_ui_snippet("09_tg_link", geo)
            if not script_sent_in_history(out, tg_sn):
                gid_sn = script_ui_snippet("07_game_id", geo)
                if script_sent_in_history(out, gid_sn) and effective_step >= 7:
                    return ["08_tg_invite", "09_tg_link"]
        if effective_step < 4 and (
            intent in ("positive", "interested", "ready") or _positive_signal()
        ):
            if not how_sent:
                return ["02_how_it_works"]
            if not link_sent:
                return _eg_reg_scripts()
        return []

    if effective_step < 3:
        if intent in ("interested", "positive", "ready", "question") or (
            wants_details_after_intro(t)
            or is_funnel_positive_reaction(t, attachments, funnel_step=effective_step)
            or is_ready_for_registration(t, geo=geo)
        ):
            return ["02_how_it_works", "03_zmw_table"]
        return []

    link_sent_global = reg_link_sent_in_history(out, geo=geo)

    if effective_step < 4:
        if is_registration_confirmed(t) and link_sent_global:
            if should_send_deposit_script(
                t, effective_step, out, folder_step=0, geo=geo
            ):
                return ["06_deposit"]
            return []
        if (
            is_ready_for_registration(t, geo=geo)
            or wants_registration_link(t)
            or intent in ("ready", "interested", "positive", "question")
        ):
            if link_sent_global and is_registration_confirmed(t):
                return ["06_deposit"] if should_send_deposit_script(
                    t, effective_step, out, folder_step=0, geo=geo
                ) else []
            if link_sent_global:
                return []
            return ["04_registration", "05_link"]
        return []

    # Link already sent (step 4+)
    if is_registration_pending(t) and not link_sent_global:
        return ["04_registration", "05_link"]
    if is_registration_pending(t) and link_sent_global:
        return []

    if effective_step < 7:
        if intent == "game_id_text":
            return []
        link_sn = script_ui_snippet("05_link", geo)
        link_sent = script_sent_in_history(out, link_sn)
        if link_sent and effective_step >= 4:
            dep_sn = script_ui_snippet("06_deposit", geo)
            if not script_sent_in_history(out, dep_sn) and (
                is_what_required_question(t)
                or is_post_link_registration_question(t)
                or intent in ("question", "positive", "interested", "ready")
            ):
                return ["06_deposit"]
        if is_registration_confirmed(t) or intent == "joined":
            if should_send_deposit_script(
                t, effective_step, out, folder_step=0, geo=geo
            ):
                return ["06_deposit"]
        return []

    if effective_step < 8 and intent == "game_id_text":
        return ["07_game_id"]

    return []


def resolve_cm_backlog_fallback(
    effective_step: int,
    outgoing_texts: list[str],
    intent: str = "unknown",
    *,
    text: str = "",
) -> list[str]:
    """CM «Без статусу» — intro → age → steps → tier → reg/link."""
    from services.ai_intent import (
        is_age_answer,
        is_deposit_tier_choice,
        is_registration_confirmed,
        is_registration_pending,
        wants_registration_link,
    )

    out = outgoing_texts or []
    t = (text or "").strip()
    geo = "cm"
    dep_key = deposit_script_key(geo)
    intro_sn = script_ui_snippet("01_intro", geo)
    intro2_sn = script_ui_snippet("01_intro_2", geo)
    age_sn = script_ui_snippet("02_age", geo)
    steps_sn = script_ui_snippet("03_steps", geo)
    tier_sn = script_ui_snippet("04_tier", geo)
    intro_sent = script_sent_in_history(out, intro_sn)
    intro2_sent = script_sent_in_history(out, intro2_sn)
    age_sent = script_sent_in_history(out, age_sn)
    steps_sent = script_sent_in_history(out, steps_sn)
    tier_sent = script_sent_in_history(out, tier_sn)
    link_sent = reg_link_sent_in_history(out, geo=geo)
    dep_sn = script_ui_snippet(dep_key, geo)

    if link_sent:
        if is_registration_confirmed(t):
            if not script_sent_in_history(out, dep_sn):
                return [dep_key]
            return []
        if (
            effective_step < 8
            and not script_sent_in_history(out, dep_sn)
            and intent
            in (
                "joined",
                "positive",
                "ready",
                "deposit_done",
                "question",
                "interested",
            )
        ):
            return [dep_key]
        return []

    if not intro_sent:
        if intent == "declined":
            return []
        if intent not in (
            "interested",
            "positive",
            "ready",
            "question",
            "unknown",
        ):
            return []
        return ["01_intro", "01_intro_2"]
    if not intro2_sent:
        return ["01_intro_2"]
    if not age_sent and effective_step < 5:
        if intent == "declined":
            return []
        return ["02_age"]
    if not steps_sent and effective_step < 5:
        if intent == "declined":
            return []
        if is_age_answer(t) or intent in (
            "positive",
            "ready",
            "interested",
            "question",
            "unknown",
        ):
            return ["03_steps"]
        return []
    if wants_registration_link(t) and steps_sent and not link_sent:
        if tier_sent:
            return ["05_registration", "06_link", "07_chrome"]
        return ["04_tier"]
    if not tier_sent and effective_step < 5:
        if intent == "declined":
            return []
        return ["04_tier"]
    if is_deposit_tier_choice(t, geo=geo):
        return ["05_registration", "06_link", "07_chrome"]
    if effective_step < 6:
        if is_registration_confirmed(t):
            return []
        if is_registration_pending(t):
            return ["05_registration", "06_link", "07_chrome"]
        return ["05_registration", "06_link", "07_chrome"]
    return []


def resolve_zm_backlog_fallback(
    effective_step: int,
    outgoing_texts: list[str],
    intent: str = "unknown",
    *,
    geo: str = "zm",
    text: str = "",
) -> list[str]:
    """ZM/DJ «Без статусу» — advance funnel when intent did not match templates."""
    if geo == "cm":
        return resolve_cm_backlog_fallback(
            effective_step, outgoing_texts, intent, text=text
        )
    from services.ai_intent import is_registration_confirmed, is_registration_pending

    out = outgoing_texts or []
    t = (text or "").strip()
    intro_sn = script_ui_snippet("01_intro", geo)
    how_sn = script_ui_snippet("02_how_it_works", geo)
    intro_sent = script_sent_in_history(out, intro_sn)
    how_sent = script_sent_in_history(out, how_sn)
    link_sent = reg_link_sent_in_history(out, geo=geo)
    dep_sn = script_ui_snippet("06_deposit", geo)

    # Link already sent — never rewind to intro / resend 04+05.
    if link_sent:
        if is_registration_confirmed(t):
            if not script_sent_in_history(out, dep_sn):
                return ["06_deposit"]
            return []
        if (
            effective_step < 8
            and not script_sent_in_history(out, dep_sn)
            and intent
            in (
                "joined",
                "positive",
                "ready",
                "deposit_done",
                "question",
                "interested",
            )
        ):
            return ["06_deposit"]
        return []

    if not intro_sent:
        if effective_step >= 1 and effective_step < 4 and not how_sent:
            return ["02_how_it_works", "03_zmw_table"]
        if intent == "declined":
            return []
        if intent not in (
            "interested",
            "positive",
            "ready",
            "question",
            "unknown",
        ):
            return []
        return ["01_intro"]
    if not how_sent and effective_step < 4:
        if intent == "declined":
            return []
        return ["02_how_it_works", "03_zmw_table"]
    if effective_step < 6:
        if is_registration_confirmed(t):
            return []
        if is_registration_pending(t):
            return ["04_registration", "05_link"]
        return ["04_registration", "05_link"]
    return []


def resolve_eg_backlog_fallback(
    effective_step: int,
    outgoing_texts: list[str],
    intent: str = "unknown",
) -> list[str]:
    """EG «Без статусу» backlog — advance funnel from history gaps when intent is unknown."""
    out = outgoing_texts or []
    intro_sn = script_ui_snippet("01_intro", "eg")
    how_sn = script_ui_snippet("02_how_it_works", "eg")
    link_sn = script_ui_snippet("05_link", "eg")
    intro_sent = script_sent_in_history(out, intro_sn)
    how_sent = script_sent_in_history(out, how_sn)
    link_sent = script_sent_in_history(out, link_sn)

    if not intro_sent:
        return ["01_intro"]
    if not how_sent and effective_step < 4:
        return ["02_how_it_works"]
    if not link_sent and effective_step < 6:
        return ["04_registration", "05_link"]
    dep_sn = script_ui_snippet("06_deposit", "eg")
    if (
        link_sent
        and effective_step < 8
        and not script_sent_in_history(out, dep_sn)
        and intent
        in (
            "joined",
            "positive",
            "ready",
            "deposit_done",
            "question",
            "interested",
        )
    ):
        return ["06_deposit"]
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
        m = re.search(r"\b(10\d{8,})\b", text or "") or re.search(
            r"\b(17\d{6,})\b", text or ""
        )
        if m:
            return m.group(1)
    elif geo in ("dj", "cm"):
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
