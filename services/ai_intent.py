"""Classify client messages — rules first, optional OpenAI."""

from __future__ import annotations

import re
from enum import Enum


class Intent(str, Enum):
    INTERESTED = "interested"
    POSITIVE = "positive"
    READY = "ready"
    JOINED = "joined"
    COMPLAINT = "complaint"
    QUESTION = "question"
    IMAGE_ONLY = "image_only"
    GAME_ID_TEXT = "game_id_text"
    UNKNOWN = "unknown"


_INTERESTED = re.compile(
    r"\b(interested|interest|312|teach me|need help|need job|i am interested|"
    r"i'm interested|tell me more|am interested|kindly explain|explain it|"
    r"your help|help me|go ahead|hi go ahead|interested please|"
    r"i'm serious|i am serious|very interested)\b",
    re.I,
)
_ACK = re.compile(
    r"\b(sure|done|ok|okay|thanks|thank you|i have done|as you said|"
    r"will do|already done|got it|understood|alright)\b",
    re.I,
)
_POSITIVE = re.compile(
    r"\b(yes|ok|okay|explain|i am|you can|sure|alright|got it|"
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
    if _ACK.search(t):
        return Intent.POSITIVE
    if _JOINED.search(t):
        return Intent.JOINED
    if _READY.search(t):
        return Intent.READY
    if _INTERESTED.search(t):
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
    # New leads in «Без статусу» — always try scripts, never escalate on unknown.
    if no_status and step < 3 and intent in (Intent.UNKNOWN, Intent.QUESTION):
        return False
    if step >= 5 and intent in (Intent.UNKNOWN, Intent.QUESTION):
        return False
    if intent in (Intent.QUESTION, Intent.UNKNOWN):
        return True
    return False


def needs_human_for_text(
    intent: Intent, step: int, text: str, *, no_status: bool = False
) -> bool:
    if is_registration_pending(text) and step < 6:
        return False
    return needs_human(intent, step, no_status=no_status)
