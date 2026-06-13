"""Default Pager status UUIDs (Zambia org — override per account in DB later)."""

from __future__ import annotations

import os

ZM_STATUSES = {
    "in_progress": "8a100b5d-98b9-4d05-abce-de2572f0bf72",  # В процесі реєстрації
    "wait_id": "eac6f92c-05ea-4de8-adfb-b258aad6f358",  # Чекаю ID
    "registration": "9742e6f1-1112-4c3e-b7a8-94aee9a57f8b",  # Реєстрація
    "deps_pending": "da62404d-c4f2-4281-bcfe-9ed5d2cbf593",  # Депи не дійшли
}

EXCELLENT = "Excellent 👍"

# Do not auto-reply in these folders; patch_status INTO them is still allowed.
SKIP_PROCESSING_STATUS_IDS: frozenset[str] = frozenset(
    {
        ZM_STATUSES["deps_pending"],
        *(s.strip() for s in (os.getenv("PAGER_STATUS_COMPLETED_ID") or "").split(",") if s.strip()),
    }
)

_SKIP_NAME_FRAGMENTS = (
    "депи не",
    "депы не",
    "deps",
    "не дійш",
    "не дошл",
    "заверш",
    "completed",
    "finished",
)


def should_skip_processing(conv: dict) -> bool:
    """Skip chats already in terminal / handoff folders."""
    status_id = str(conv.get("statusId") or "").strip()
    if status_id and status_id in SKIP_PROCESSING_STATUS_IDS:
        return True
    name = ((conv.get("status") or {}).get("name") or "").strip().lower()
    return any(frag in name for frag in _SKIP_NAME_FRAGMENTS)
