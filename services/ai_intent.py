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
    r"\b(interested|interest|312|teach me|need help|i am interested|"
    r"tell me more|am interested|kindly explain|explain it)\b",
    re.I,
)
_ACK = re.compile(
    r"\b(sure|done|ok|okay|thanks|thank you|i have done|as you said|"
    r"will do|already done|got it|understood|alright)\b",
    re.I,
)
_POSITIVE = re.compile(r"\b(explain|i am)\b", re.I)
_READY = re.compile(r"\b(yes|ready|am ready|let'?s start|start today)\b", re.I)
_JOINED = re.compile(r"\b(have joined|joined|i joined)\b", re.I)
_COMPLAINT = re.compile(
    r"\b(lost|didn'?t win|scam|taking my money|stop|refund|nothing happened)\b", re.I
)
_GAME_ID = re.compile(r"\b16\d{6,}\b")


def classify(text: str, *, has_image: bool = False) -> Intent:
    t = (text or "").strip()
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
    if _READY.search(t) and len(t) < 40:
        return Intent.READY
    if _INTERESTED.search(t):
        return Intent.INTERESTED
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
