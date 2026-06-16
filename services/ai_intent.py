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
    r"i'm serious|i am serious|very interested|yess?\s+sir)\b",
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
    r"\b(lost|didn'?t win|scam|taking my money|stop|refund|nothing happened)\b", re.I
)
_GAME_ID = re.compile(r"\b16\d{6,}\b")
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
    if re.fullmatch(r"explain\??", t, re.I):
        return True
    return bool(_REGISTRATION_FOLLOWUP.search(t))


def is_deferral_reply(text: str) -> bool:
    """Client postpones — do not send registration or game ID scripts."""
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"no\.?", t, re.I):
        return True
    return bool(_DEFERRAL.search(t))


def is_registration_complete(text: str) -> bool:
    """Client finished registration — OK to send deposit script."""
    t = (text or "").strip()
    if not t:
        return False
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
    t = re.sub(r"[^\w\s]", "", (text or "").strip())
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


def is_ready_for_registration(text: str) -> bool:
    """After 02+03 — client wants reg link (not vague okay / later)."""
    t = (text or "").strip()
    if not t or is_deferral_reply(t):
        return False
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


def classify(
    text: str, *, has_image: bool = False, has_ad: bool = False
) -> Intent:
    t = (text or "").strip()
    if has_ad and not t and not has_image:
        return Intent.INTERESTED
    if has_image and not t:
        return Intent.IMAGE_ONLY
    if _GAME_ID.search(t):
        return Intent.GAME_ID_TEXT
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
    intent: Intent, step: int, text: str, *, no_status: bool = False
) -> bool:
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
