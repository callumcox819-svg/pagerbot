"""Refresh Pager session cookies for worker accounts."""

from __future__ import annotations

import json
import logging
from typing import Any

import database as db
from config import load_settings, resolve_pager_org_id, resolve_operator_user_id
from services.encryption import Secrets
from services.pager_api import PagerAPIError, PagerClient, is_session_error
from services.pager_auth import authenticate

logger = logging.getLogger(__name__)
_settings = load_settings()
_secrets = Secrets(_settings.encryption_key)

async def refresh_pager_session(account: dict[str, Any]) -> dict[str, str] | None:
    """Re-login with stored email/password and persist new cookies."""
    email = str(account.get("email") or "").strip()
    pwd_enc = str(account.get("password_enc") or "").strip()
    if not email or not pwd_enc:
        logger.warning(
            "Cannot refresh session account=%s: missing email or password",
            account.get("id"),
        )
        return None

    password = _secrets.decrypt(pwd_enc)
    try:
        result = await authenticate(email=email, password=password)
    except Exception as exc:
        logger.warning(
            "Session refresh failed account=%s: %s",
            account.get("id"),
            exc,
        )
        await db.upsert_account(
            int(account["tg_user_id"]),
            session_ok=0,
            last_error=f"Session refresh failed: {exc}",
        )
        return None

    cookies = dict(result["cookies"])
    org_hint = str(
        cookies.get("_pager_org_id")
        or account.get("org_id")
        or ""
    ).strip()
    user_hint = str(
        cookies.get("_pager_user_id")
        or account.get("pager_user_id")
        or ""
    ).strip()
    org_slug = str(account.get("org_slug") or _settings.pager_org_slug or "").strip()
    org_id = resolve_pager_org_id(
        org_hint or str(account.get("org_id") or ""),
        _settings.pager_org_id,
        org_slug=org_slug,
    )
    client = PagerClient(
        _settings.pager_base_url,
        cookies,
        org_id=org_id,
        org_slug=org_slug,
        locale=str(account.get("pager_locale") or _settings.pager_locale),
        org_id_fallback=org_id,
    )
    try:
        probe = await client.probe_session()
    except PagerAPIError as exc:
        logger.warning(
            "Refreshed cookies still invalid account=%s: %s",
            account.get("id"),
            exc,
        )
        await db.upsert_account(
            int(account["tg_user_id"]),
            session_ok=0,
            last_error="Session refresh probe failed",
        )
        return None

    session_enc = _secrets.encrypt(json.dumps(cookies))
    operator_id = resolve_operator_user_id(
        _settings.pager_user_id,
        user_hint,
        probe.get("pager_user_id"),
        org_slug=org_slug,
    )
    await db.upsert_account(
        int(account["tg_user_id"]),
        session_enc=session_enc,
        org_id=probe.get("org_id") or org_id,
        org_slug=probe.get("org_slug") or org_slug,
        pager_user_id=operator_id,
        session_ok=1,
        last_error="",
    )
    acc = await db.get_account_by_tg(int(account["tg_user_id"]))
    if acc:
        cleared = await db.clear_pauses_for_account(int(acc["id"]))
        if cleared:
            logger.info(
                "Session refresh: cleared %s paused chats account=%s",
                cleared,
                account.get("id"),
            )
    logger.info(
        "Session refreshed account=%s org=%s",
        account.get("id"),
        (probe.get("org_id") or org_id or "")[:20],
    )
    return cookies
