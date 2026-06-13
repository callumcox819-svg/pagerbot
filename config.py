"""Application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"

# Known org for tehsup deployment (override via PAGER_ORG_ID)
DEFAULT_ORG_ID_BY_SLUG: dict[str, str] = {
    "tehsup": "org_3Cd5AHJTskSRAzLNkoft2qlfaUw",
}

# Operator user for tehsup (Тех Саппорт) — used for take-chat + send.
DEFAULT_USER_ID_BY_SLUG: dict[str, str] = {
    "tehsup": "user_3Cd53IT5ABAj5qZlk3qclPu6bTx",
}


def resolve_pager_org_id(*values: str, org_slug: str = "") -> str:
    """Pick first valid org_ id, else map slug / env."""
    for raw in values:
        v = (raw or "").strip()
        if v.startswith("org_"):
            return v
    slug = (org_slug or os.getenv("PAGER_ORG_SLUG") or "").strip().lower()
    if slug in DEFAULT_ORG_ID_BY_SLUG:
        return DEFAULT_ORG_ID_BY_SLUG[slug]
    env = (os.getenv("PAGER_ORG_ID") or "").strip()
    if env.startswith("org_"):
        return env
    return ""


def resolve_operator_user_id(*values: str, org_slug: str = "") -> str:
    """Тех Саппорт operator id — never use Clerk/page identity for send."""
    for raw in values:
        v = (raw or "").strip()
        if v.startswith("user_"):
            return v
    env = (os.getenv("PAGER_USER_ID") or "").strip()
    if env.startswith("user_"):
        return env
    slug = (org_slug or os.getenv("PAGER_ORG_SLUG") or "").strip().lower()
    return DEFAULT_USER_ID_BY_SLUG.get(slug, "")


@dataclass
class Settings:
    bot_token: str
    encryption_key: str
    openai_api_key: str
    admin_ids: set[int]
    poll_sec: float
    db_path: Path
    pager_base_url: str = "https://www.pager.co.ua"
    pager_locale: str = "uk"
    pager_org_slug: str = ""
    pager_org_id: str = ""
    pager_user_id: str = ""


def load_settings() -> Settings:
    admins_raw = (os.getenv("ADMIN_IDS") or "").strip()
    admin_ids: set[int] = set()
    for part in admins_raw.split(","):
        part = part.strip()
        if part.isdigit():
            admin_ids.add(int(part))

    enc = (os.getenv("ENCRYPTION_KEY") or "").strip()
    if not enc:
        raise RuntimeError("ENCRYPTION_KEY is required in .env")

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is required in .env")

    db = os.getenv("DATABASE_PATH", "data/bot.db").strip()
    poll = float(os.getenv("PAGER_POLL_SEC", "45"))

    pager_org_slug = (os.getenv("PAGER_ORG_SLUG") or "").strip()
    pager_org_id = (os.getenv("PAGER_ORG_ID") or "").strip()
    if not pager_org_id and pager_org_slug:
        pager_org_id = DEFAULT_ORG_ID_BY_SLUG.get(pager_org_slug.lower(), "")
    pager_user_id = (os.getenv("PAGER_USER_ID") or "").strip()
    if not pager_user_id and pager_org_slug:
        pager_user_id = DEFAULT_USER_ID_BY_SLUG.get(pager_org_slug.lower(), "")

    return Settings(
        bot_token=token,
        encryption_key=enc,
        openai_api_key=(os.getenv("OPENAI_API_KEY") or "").strip(),
        admin_ids=admin_ids,
        poll_sec=poll,
        db_path=ROOT / db,
        pager_locale=(os.getenv("PAGER_LOCALE") or "uk").strip() or "uk",
        pager_org_slug=pager_org_slug,
        pager_org_id=pager_org_id,
        pager_user_id=pager_user_id,
    )
