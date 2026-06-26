"""Pager funnel status UUIDs — defaults for ZM org; resolved per account from DB names."""

from __future__ import annotations

import os
from typing import Any

ZM_STATUSES = {
    "in_progress": "8a100b5d-98b9-4d05-abce-de2572f0bf72",  # В процесі реєстрації
    "wait_id": "eac6f92c-05ea-4de8-adfb-b258aad6f358",  # Чекаю ID
    "registration": "9742e6f1-1112-4c3e-b7a8-94aee9a57f8b",  # Реєстрація
    "deps_pending": "da62404d-c4f2-4281-bcfe-9ed5d2cbf593",  # Депи не дійшли
}

_STATUS_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "in_progress": ("процесі", "процесс", "in progress", "реєстрації"),
    "wait_id": ("чекаю id", "чекаю ід", "wait id", "wait_id"),
    "registration": ("рега", "реєстрація", "регистрация", "registration"),
    "deps_pending": ("депи не", "депы не", "deps pending", "deps"),
    "completed": ("заверш", "completed", "finish", "terminé", "termine"),
}

FUNNEL_GEOS = frozenset({"zm", "eg", "dj", "cm"})

# Funnel folders where bot continues scripts (after «Без статусу»).
ACTIVE_FUNNEL_STATUS_IDS: frozenset[str] = frozenset(
    {
        ZM_STATUSES["in_progress"],
        ZM_STATUSES["wait_id"],
        ZM_STATUSES["registration"],
    }
)


def resolve_funnel_statuses(
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Map funnel keys to status UUIDs — DB name match, else ZM defaults."""
    out = dict(ZM_STATUSES)
    if not rows:
        return out
    for key, hints in _STATUS_NAME_HINTS.items():
        for st in rows:
            sid = str(st.get("status_id") or "").strip()
            name = (st.get("name") or "").strip().lower()
            if sid and name and any(h in name for h in hints):
                out[key] = sid
                break
    return out


def funnel_status_ids(funnel_statuses: dict[str, str] | None = None) -> frozenset[str]:
    fs = funnel_statuses or ZM_STATUSES
    return frozenset(
        fs[k]
        for k in ("in_progress", "wait_id", "registration")
        if fs.get(k)
    )

NO_STATUS_FOLDER_ID = ""

# Virtual folder — Pager inbox tab «Всі» / «Все» (all statuses, one channel scan).
ALL_INBOX_FOLDER_ID = "*"


def normalize_enabled_folders(enabled: set[str] | None) -> tuple[set[str], bool]:
    """Split folder picker state into specific ids vs «Всі».

    If both «Всі» and specific folders are enabled, specific folders win
    (user toggled «Без статусу» without turning off «Всі» in DB).
    """
    if not enabled:
        return set(), False
    specific = {str(x) for x in enabled if str(x) != ALL_INBOX_FOLDER_ID}
    all_inbox = ALL_INBOX_FOLDER_ID in enabled
    if specific:
        return specific, False
    if all_inbox:
        return set(), True
    return set(), False


def conv_folder_key(conv: dict) -> str:
    """Map conversation to folder id used in pager_channel_folders."""
    if is_no_status(conv):
        return NO_STATUS_FOLDER_ID
    return str(conv.get("statusId") or "").strip()


def conv_allowed_in_folders(conv: dict, enabled: set[str] | None) -> bool:
    specific, all_inbox = normalize_enabled_folders(enabled)
    if all_inbox:
        return True
    if not specific:
        return False
    return conv_folder_key(conv) in specific

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


def should_skip_processing(
    conv: dict, funnel_statuses: dict[str, str] | None = None
) -> bool:
    """Skip chats in terminal / handoff folders."""
    fs = funnel_statuses or ZM_STATUSES
    status_id = str(conv.get("statusId") or "").strip()
    completed_sid = str(fs.get("completed") or "").strip()
    if completed_sid and status_id == completed_sid:
        return False
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


def folder_callback_token(status_id: str) -> str:
    """Encode folder id for Telegram callback_data (fld:t:…)."""
    sid = status_id if status_id is not None else ""
    if sid == NO_STATUS_FOLDER_ID:
        return "ns"
    if sid == ALL_INBOX_FOLDER_ID:
        return "all"
    return sid


def folder_callback_decode(token: str) -> str:
    """Decode fld:t: callback token back to folder status_id."""
    t = (token or "").strip()
    if t == "ns":
        return NO_STATUS_FOLDER_ID
    if t == "all":
        return ALL_INBOX_FOLDER_ID
    return t


def should_process_conversation(
    conv: dict,
    *,
    geo: str = "zm",
    funnel_statuses: dict[str, str] | None = None,
    allowed_folders: set[str] | None = None,
) -> bool:
    """Process chats in «Без статусу» + funnel folders enabled in 📂 picker (or env)."""
    if should_skip_processing(conv, funnel_statuses):
        return False
    if is_no_status(conv):
        return True
    status_id = str(conv.get("statusId") or "").strip()
    if allowed_folders is not None:
        specific, all_inbox = normalize_enabled_folders(allowed_folders)
        if not all_inbox:
            if specific == {NO_STATUS_FOLDER_ID}:
                return False
            if status_id and status_id in specific:
                return True
    active = funnel_status_ids(funnel_statuses)
    completed_sid = str((funnel_statuses or ZM_STATUSES).get("completed") or "").strip()
    if completed_sid and status_id == completed_sid:
        if allowed_folders is not None:
            specific, all_inbox = normalize_enabled_folders(allowed_folders)
            wait_sid = str((funnel_statuses or ZM_STATUSES).get("wait_id") or "").strip()
            if all_inbox or conv_allowed_in_folders(conv, allowed_folders):
                return True
            if wait_sid and wait_sid in specific:
                return True
        return False
    if status_id not in active:
        return False
    if process_funnel_folders():
        return True
    if allowed_folders is not None and conv_allowed_in_folders(conv, allowed_folders):
        return True
    return False


def infer_step_from_status(
    conv: dict,
    funnel_statuses: dict[str, str] | None = None,
) -> int:
    """Minimum funnel step implied by Pager folder (link sent, waiting ID, etc.)."""
    fs = funnel_statuses or ZM_STATUSES
    status_id = str(conv.get("statusId") or "").strip()
    if status_id == fs.get("in_progress"):
        return 4
    if status_id == fs.get("wait_id"):
        return 6
    if status_id == fs.get("registration"):
        return 7
    return 0
