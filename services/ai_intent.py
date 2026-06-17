"""Classify client messages — rules first, optional OpenAI."""

from __future__ import annotations

import re
from enum import Enum


class Intent(str, Enum):
    INTERESTED = "interested"
    POSITIVE = "positive"
    READY = "ready"
    JOINED = "joined"
    DEPOSIT_DONE = "deposit_done"
    COMPLAINT = "complaint"
    QUESTION = "question"
    IMAGE_ONLY = "image_only"
    GAME_ID_TEXT = "game_id_text"
    UNKNOWN = "unknown"


_INTERESTED = re.compile(
    r"\b(interested|interest|312|teach me|need help|need job|i am interested|"
    r"i'm interested|tell me more|am interested|kindly explain|explain it|"
    r"your help|help me|go ahead|hi go ahead|interested please|"
    r"i'm serious|i am serious|very interested|yess?\s+sir|"
    r"would like to join|would love to join|wanna join|want to join|"
    r"like to join|count me in)\b",
    re.I,
)
_GREETING = re.compile(
    r"\b(good (morning|afternoon|evening)|hello|hi|hey)\b", re.I
)
_ACK = re.compile(
    r"\b(sure|done|ok|okay|thanks|thank you|i have done|as you said|"
    r"will do|already done|got it|understood|alright)\b",
    re.I,
)
_POSITIVE = re.compile(
    r"\b(yess?|ok|okay|explain|i am|you can|sure|alright|got it|"
    r"how can i start|how do i start|how to start)\b",
    re.I,
)
_READY = re.compile(
    r"\b(am ready|i'?m ready|let'?s start|start today|ready to start)\b", re.I
)
_JOINED = re.compile(r"\b(have joined|joined|i joined)\b", re.I)
_COMPLAINT = re.compile(
    r"\b(lost|losed|didn'?t win|scam|taking my money|stop|refund|nothing happened|lied)\b",
    re.I,
)
_GAME_ID = re.compile(r"\b16\d{6,}\b")
_GAME_ID_EG = re.compile(r"\b17\d{6,}\b")
_ARABIC = re.compile(r"[\u0600-\u06FF]")
_AR_INTERESTED = re.compile(
    r"مهتم|اهتم|عايز|عاوز|حابب|ابي|أبي|عاوزه|عايزه|محتاج|مساعد|ساعد|ممكن"
)
_POSITIVE_EMOJI = re.compile(
    r"^[\s"
    r"\U0001F44D\U0001F44C\U0001F44F\U0001F600-\U0001F64F"
    r"\U0001F970\U0001F60A\U0001F603\U00002764\U00002705\U0001F49D"
    r"\U0001F64F\U0001F4AF"
    r"👍👌❤✅🙏😊😄💯🙂😆❤️👏"
    r"]+$"
)


def is_positive_emoji_only(text: str) -> bool:
    """Thumbs-up / heart / smiley only — treat as yes after intro."""
    t = (text or "").strip()
    if not t:
        return False
    if t in (
        "👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿",
        "👌", "❤", "❤️", "💯", "🙂", "😊", "😄", "🙏", "👏", "✅",
    ):
        return True
    return bool(_POSITIVE_EMOJI.match(t))


def is_messenger_reaction_attachment(attachments: list) -> bool:
    """Facebook like / sticker — not a deposit or ID screenshot."""
    for att in attachments or []:
        typ = (att.get("type") or "").lower()
        if typ in (
            "sticker",
            "like",
            "thumbs_up",
            "emoji",
            "fallback",
            "reaction",
        ):
            return True
        if typ == "image":
            payload = att.get("payload") or {}
            if payload.get("sticker_id") or att.get("sticker_id"):
                return True
            url = (payload.get("url") or "").lower()
            if any(
                x in url
                for x in (
                    "sticker",
                    "/t39.1997",
                    "reaction",
                    "like_thumb",
                    "thumbs",
                    "emoji.php",
                )
            ):
                return True
            w = payload.get("width") or att.get("width")
            h = payload.get("height") or att.get("height")
            try:
                if w and h and int(w) <= 160 and int(h) <= 160:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def is_funnel_positive_reaction(
    text: str, attachments: list | None = None, *, funnel_step: int = 0
) -> bool:
    """Early funnel — emoji / FB like means «yes, continue»."""
    if funnel_step >= 4:
        return False
    if is_positive_emoji_only(text):
        return True
    if not (text or "").strip() and is_messenger_reaction_attachment(
        attachments or []
    ):
        return True
    return False


_AR_POSITIVE = re.compile(
    r"تمام|أيوه|ايوه|آه|اه|اوك|نعم|يلا|بينا|ماشي|حاضر|طيب|كويس|"
    r"ياريت|اكيد|أكيد|طبعا|حلو|جميل"
)
_AR_DETAILS = re.compile(
    r"قولي|قول|تفاصيل|فهمني|اشرح|علمني|ازاي|إزاي|وضح|فهمني"
)
_AR_READY = re.compile(r"جاهز|هبدأ|نبدأ|ابدأ|جاهزين|يلا بينا")
_AR_REG_COMPLETE = re.compile(
    r"خلصت|سجلت|انهيت|انتهيت|عملت حساب|فتحت حساب|عملت التسجيل|خلصت التسجيل"
)
_AR_REG_PENDING = re.compile(
    r"لسه|مش خلصت|مش سجلت|بسجل|هسجل|بعمل|لسا|لسه مش|مش عملت"
)
_AR_DEFERRAL = re.compile(
    r"بكرة|كمان شويه|مش دلوقتي|بعدين|مش جاهز|مش دلوقت|بعدين"
)
_AR_DEPOSIT = re.compile(
    r"إيداع|ايداع|عملت إيداع|عملت ايداع|حطيت|ودعت|عملت ديبوزيت"
)
_AR_COMPLAINT = re.compile(r"نصب|كذب|خسارة|سرق|احتيال|غش")
_AR_APP = re.compile(
    r"تطبيق|متصفح|انزل|البرنامج|الابليكيشن|كروم|chrome|download|app"
)
_AR_REG_LINK = re.compile(
    r"فين.*(حساب|سجل|تسجيل|لينك|رابط)|"
    r"(اعمل|أعمل|عمل).*(حساب|تسجيل)|"
    r"(اللينك|الرابط|لينك|رابط)|"
    r"ازاي.*(سجل|تسجيل|حساب)"
)
_REGISTRATION_FOLLOWUP = re.compile(
    r"\b(explain|how can i start|how do i start|how to start|tell me how|"
    r"how does it work|how it works|what do i do|what should i do|"
    r"what is next|what's next|next step|get started|start now)\b",
    re.I,
)
_DEFERRAL = re.compile(
    r"\b(let you know|when i'?m ready|not ready|maybe later|tomorrow|"
    r"next day|another day|only today|hand cash|unfortunately|"
    r"will tell you|get back to you|not now|not today|later on|"
    r"give me time|need time|only have)\b",
    re.I,
)
_REG_COMPLETE = re.compile(
    r"\b(registered|registration done|done registering|done with registration|"
    r"i registered|have registered|finished registering|signed up|account created|"
    r"created (my |an )?account|i have registered)\b",
    re.I,
)
_DEPOSIT_TIER = re.compile(r"^(30|50|100|200|300|500|1000|2000)$")


def is_commitment_reply(text: str) -> bool:
    """Short affirmations after intro / ZMW table (e.g. Yes, I'm serious)."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"yes\.?", t, re.I):
        return True
    return bool(
        re.search(
            r"\b(i'?m serious|i am serious|very interested|interested please|"
            r"yes please|count me in|let'?s go|lets go|want to start|"
            r"please explain|tell me more|go ahead|i'?m in|sign me up|"
            r"i want in|ready when you are)\b",
            t,
            re.I,
        )
    )


def wants_registration_followup(text: str) -> bool:
    """After intro — treat start/how/explain questions like a positive reply."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_REG_LINK.search(t):
        return True
    if re.fullmatch(r"explain\??", t, re.I):
        return True
    return bool(_REGISTRATION_FOLLOWUP.search(t))


def wants_registration_link(text: str) -> bool:
    """Client asks where/how to register — send 04+05, not 02 again."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_AR_REG_LINK.search(t)) or wants_registration_followup(t)


def is_deferral_reply(text: str) -> bool:
    """Client postpones — do not send registration or game ID scripts."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"no\.?", t, re.I):
        return True
    if _AR_DEFERRAL.search(t):
        return True
    return bool(_DEFERRAL.search(t))


def is_registration_complete(text: str) -> bool:
    """Client finished registration — OK to send deposit script."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_REG_COMPLETE.search(t):
        return True
    return bool(_REG_COMPLETE.search(t))


def is_on_registration_site(text: str) -> bool:
    """Client opened the reg link / is on 1xbet."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(
        re.search(
            r"\b(1xbet|xbet|on the site|opened the link|opening the link|"
            r"taking me|it'?s taking me|i'?m on|went to the link)\b",
            t,
            re.I,
        )
    )


def is_post_reg_ack(text: str) -> bool:
    """Short yes after reg link — treat as registered, send deposit script."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _AR_DETAILS.search(raw):
        return False
    if _AR_POSITIVE.search(raw) and len(raw.split()) <= 3:
        return True
    t = re.sub(r"[^\w\s]", "", raw)
    if not t:
        return False
    return bool(
        re.fullmatch(
            r"(yeah|yes|yep|yess|ok|okay|sure|alright|done|finished)\s*",
            t,
            re.I,
        )
    )


def is_registration_confirmed(text: str) -> bool:
    """After 04+05 — client registered or clearly on the site."""
    return (
        is_registration_complete(text)
        or is_on_registration_site(text)
        or is_post_reg_ack(text)
    )


def is_deposit_tier_choice(text: str) -> bool:
    """Answer to ZMW table (30 / 50 / 100…) — treat as ready for registration."""
    return bool(_DEPOSIT_TIER.match((text or "").strip()))


def wants_details_after_intro(text: str) -> bool:
    """After intro — client asks for explanation."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_DETAILS.search(t):
        return True
    return wants_registration_followup(t)


def is_app_or_browser_question(text: str) -> bool:
    """Client asks app vs browser — send 08_app_or_browser."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_APP.search(t):
        return True
    return bool(
        re.search(
            r"\b(app|browser|download|install|play store|apk)\b",
            t,
            re.I,
        )
    )


def is_ready_for_registration(text: str) -> bool:
    """After 02+03 — client wants reg link (not vague okay / later)."""
    t = (text or "").strip()
    if not t or is_deferral_reply(t):
        return False
    if _AR_READY.search(t):
        return True
    if _AR_POSITIVE.search(t) and len(t.split()) <= 5 and not _AR_DETAILS.search(t):
        return True
    if is_deposit_tier_choice(t):
        return True
    if re.fullmatch(r"yes\.?", t, re.I):
        return True
    if is_commitment_reply(t):
        return True
    if _READY.search(t):
        return True
    if wants_registration_followup(t):
        return True
    if _INTERESTED.search(t) and "explain" in t.lower():
        return True
    if re.fullmatch(r"explain\??", t, re.I):
        return True
    # Short ack only — not enough for registration link
    if _ACK.search(t) and len(t.split()) <= 4:
        return False
    if _POSITIVE.search(t) and not _DEFERRAL.search(t):
        if len(t.split()) > 5:
            return False
        return True
    return False


def is_deposit_confirmation(text: str) -> bool:
    """Client says they deposited — never resend registration scripts."""
    t = (text or "").strip()
    if not t:
        return False
    if _AR_DEPOSIT.search(t):
        return True
    return bool(
        re.search(
            r"\b(done deposit|deposit done|deposited|made (a |my )?deposit|"
            r"i (have |'?ve )?deposited|sent deposit|finished deposit|"
            r"completed deposit|deposit complete|paid deposit|i paid)\b",
            t,
            re.I,
        )
    )


def is_registration_pending(text: str) -> bool:
    """Client has not registered yet — resend registration + link."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"(no|not)\s+yet\.?", t, re.I):
        return True
    if _AR_REG_PENDING.search(t):
        return True
    return bool(
        re.search(
            r"\b(not yet|no yet|haven'?t yet|havent yet|have not yet|"
            r"not registered|no registration|didn'?t register|"
            r"haven'?t registered|still working|will do|doing it|"
            r"not done|not finished|no i haven'?t|not for now|"
            r"give me time|need time|later today|maybe later|"
            r"still trying|working on it|in progress)\b",
            t,
            re.I,
        )
    )


def is_reg_confirmed_funnel_message(text: str, step: int) -> bool:
    """After reg link — client on site or confirmed; send 06_deposit."""
    if step < 4 or step >= 7:
        return False
    if is_registration_pending(text):
        return False
    return is_registration_confirmed(text)


def _classify_arabic(t: str) -> Intent | None:
    if not t or not _ARABIC.search(t):
        return None
    if _GAME_ID_EG.search(t):
        return Intent.GAME_ID_TEXT
    if _AR_COMPLAINT.search(t):
        return Intent.COMPLAINT
    if _AR_DEPOSIT.search(t):
        return Intent.DEPOSIT_DONE
    if is_registration_confirmed(t):
        return Intent.POSITIVE
    if is_deferral_reply(t):
        return Intent.UNKNOWN
    if is_app_or_browser_question(t):
        return Intent.QUESTION
    if _AR_INTERESTED.search(t) or _AR_DETAILS.search(t):
        return Intent.INTERESTED
    if _AR_READY.search(t):
        return Intent.READY
    if _AR_POSITIVE.search(t):
        return Intent.POSITIVE
    if _AR_GREETING.search(t) and len(t.split()) <= 8:
        return Intent.INTERESTED
    if "؟" in t or "?" in t:
        return Intent.QUESTION
    return None


def classify(
    text: str,
    *,
    has_image: bool = False,
    has_ad: bool = False,
    geo: str = "zm",
    attachments: list | None = None,
    funnel_step: int = 0,
) -> Intent:
    t = (text or "").strip()
    if is_funnel_positive_reaction(
        t, attachments, funnel_step=funnel_step
    ):
        return Intent.POSITIVE
    if is_positive_emoji_only(t):
        return Intent.POSITIVE
    if not t and is_messenger_reaction_attachment(attachments or []):
        return Intent.POSITIVE
    if has_ad and not t and not has_image:
        return Intent.INTERESTED
    if has_image and not t:
        if is_messenger_reaction_attachment(attachments or []):
            return Intent.POSITIVE
        if funnel_step >= 1 and funnel_step < 4:
            return Intent.POSITIVE
        return Intent.IMAGE_ONLY
    game_re = _GAME_ID_EG if geo == "eg" else _GAME_ID
    if game_re.search(t):
        return Intent.GAME_ID_TEXT
    if geo == "eg" or _ARABIC.search(t):
        ar = _classify_arabic(t)
        if ar is not None:
            return ar
    if _COMPLAINT.search(t):
        return Intent.COMPLAINT
    if is_deposit_confirmation(t):
        return Intent.DEPOSIT_DONE
    if is_registration_confirmed(t):
        return Intent.POSITIVE
    if is_deferral_reply(t):
        return Intent.UNKNOWN
    if is_deposit_tier_choice(t):
        return Intent.READY
    if _ACK.search(t):
        return Intent.POSITIVE
    if _JOINED.search(t):
        return Intent.JOINED
    if _READY.search(t):
        return Intent.READY
    if _INTERESTED.search(t):
        return Intent.INTERESTED
    if _GREETING.search(t) and len(t.split()) <= 6:
        return Intent.INTERESTED
    if re.fullmatch(r"how\??", t.strip(), re.I):
        return Intent.QUESTION
    if _POSITIVE.search(t):
        return Intent.POSITIVE
    if "?" in t or re.search(r"\b(how|what|when|why|can you)\b", t, re.I):
        return Intent.QUESTION
    if t:
        return Intent.UNKNOWN
    return Intent.UNKNOWN


def needs_human(intent: Intent, step: int, *, no_status: bool = False) -> bool:
    if intent == Intent.COMPLAINT:
        return True
    # Funnel steps 1–3 — try scripts, never escalate on unknown/no.
    if step < 4 and intent in (Intent.UNKNOWN, Intent.QUESTION):
        return False
    if step >= 5 and intent in (Intent.UNKNOWN, Intent.QUESTION):
        return False
    if intent in (Intent.QUESTION, Intent.UNKNOWN):
        return True
    return False


def needs_human_for_text(
    intent: Intent, step: int, text: str, *, no_status: bool = False, geo: str = "zm"
) -> bool:
    if geo == "eg" and step < 4 and intent in (Intent.UNKNOWN, Intent.QUESTION):
        return False
    if is_deferral_reply(text) and step < 6:
        return False
    if is_reg_confirmed_funnel_message(text, step):
        return False
    if is_registration_pending(text) and step < 6:
        return False
    if is_ready_for_registration(text) and step < 5:
        return False
    if is_deposit_tier_choice(text) and step < 5:
        return False
    return needs_human(intent, step, no_status=no_status)
