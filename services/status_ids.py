"""Default Pager status UUIDs (Zambia org — override per account in DB later)."""

from __future__ import annotations

import os

ZM_STATUSES = {
    "in_progress": "8a100b5d-98b9-4d05-abce-de2572f0bf72",  # В процесі реєстрації
    "wait_id": "eac6f92c-05ea-4de8-adfb-b258aad6f358",  # Чекаю ID
    "registration": "9742e6f1-1112-4c3e-b7a8-94aee9a57f8b",  # Реєстрація
    "deps_pending": "da62404d-c4f2-4281-bcfe-9ed5d2cbf593",  # Депи не дійшли
}

# Funnel folders where bot continues scripts (after «Без статусу»).
ACTIVE_FUNNEL_STATUS_IDS: frozenset[str] = frozenset(
    {
        ZM_STATUSES["in_progress"],
        ZM_STATUSES["wait_id"],
        ZM_STATUSES["registration"],
    }
)

NO_STATUS_FOLDER_ID = ""

EXCELLENT = "Excellent 👍"

# Terminal folders — do not auto-reply; patch_status INTO them is still allowed.
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
    "скасован",
    "cancelled",
    "думают",
    "думають",
    "немає грошей",
)


def is_no_status(conv: dict) -> bool:
    """«Без статусу» — new / unprocessed leads."""
    if conv.get("statusId") in (None, ""):
        return True
    name = ((conv.get("status") or {}).get("name") or "").strip().lower()
    return "без статус" in name or name in ("", "—", "-")


def should_skip_processing(conv: dict) -> bool:
    """Skip chats in terminal / handoff folders."""
    status_id = str(conv.get("statusId") or "").strip()
    if status_id and status_id in SKIP_PROCESSING_STATUS_IDS:
        return True
    name = ((conv.get("status") or {}).get("name") or "").strip().lower()
    return any(frag in name for frag in _SKIP_NAME_FRAGMENTS)


def process_funnel_folders() -> bool:
    """Continue scripts in «В процесі» / «Чекаю ID» etc. (off by default)."""
    return os.getenv("PAGER_PROCESS_FUNNEL_FOLDERS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def should_process_conversation(conv: dict, *, geo: str = "zm") -> bool:
    """Process new leads in «Без статусу»; funnel folders only if enabled."""
    if should_skip_processing(conv):
        return False
    if geo == "eg":
        return True
    if is_no_status(conv):
        return True
    if not process_funnel_folders():
        return False
    status_id = str(conv.get("statusId") or "").strip()
    return status_id in ACTIVE_FUNNEL_STATUS_IDS


def infer_step_from_status(conv: dict) -> int:
    """Minimum funnel step implied by Pager folder (link sent, waiting ID, etc.)."""
    status_id = str(conv.get("statusId") or "").strip()
    if status_id == ZM_STATUSES["in_progress"]:
        return 4
    if status_id == ZM_STATUSES["wait_id"]:
        return 6
    if status_id == ZM_STATUSES["registration"]:
        return 7
    return 0
