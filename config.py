"""Application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "data" / "scripts"


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

    return Settings(
        bot_token=token,
        encryption_key=enc,
        openai_api_key=(os.getenv("OPENAI_API_KEY") or "").strip(),
        admin_ids=admin_ids,
        poll_sec=poll,
        db_path=ROOT / db,
        pager_locale=(os.getenv("PAGER_LOCALE") or "uk").strip() or "uk",
        pager_org_slug=(os.getenv("PAGER_ORG_SLUG") or "").strip(),
    )
