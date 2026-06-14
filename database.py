"""SQLite persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from config import load_settings

_settings = load_settings()
DB_PATH = _settings.db_path


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pager_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER NOT NULL UNIQUE,
                email TEXT NOT NULL DEFAULT '',
                password_enc TEXT NOT NULL DEFAULT '',
                session_enc TEXT NOT NULL DEFAULT '',
                org_id TEXT NOT NULL DEFAULT '',
                pager_user_id TEXT NOT NULL DEFAULT '',
                geo TEXT NOT NULL DEFAULT 'zm',
                auto_reply INTEGER NOT NULL DEFAULT 1,
                paused INTEGER NOT NULL DEFAULT 0,
                escalation_chat_id INTEGER NOT NULL DEFAULT 0,
                session_ok INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS pager_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(account_id, channel_id),
                FOREIGN KEY (account_id) REFERENCES pager_accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS conversation_states (
                account_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                step INTEGER NOT NULL DEFAULT 0,
                human_takeover INTEGER NOT NULL DEFAULT 0,
                pause_scripts INTEGER NOT NULL DEFAULT 0,
                extracted_game_id TEXT NOT NULL DEFAULT '',
                last_processed_msg_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (account_id, conversation_id),
                FOREIGN KEY (account_id) REFERENCES pager_accounts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_channels_account ON pager_channels(account_id);
            """
        )
        for stmt in (
            "ALTER TABLE pager_accounts ADD COLUMN org_slug TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE pager_accounts ADD COLUMN pager_locale TEXT NOT NULL DEFAULT 'uk'",
            "ALTER TABLE conversation_states ADD COLUMN send_failures INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE conversation_states ADD COLUMN last_escalation_msg_id TEXT NOT NULL DEFAULT ''",
        ):
            try:
                await db.execute(stmt)
            except aiosqlite.OperationalError:
                pass
        await db.commit()


async def get_account_by_tg(tg_user_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM pager_accounts WHERE tg_user_id = ?", (tg_user_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def upsert_account(
    tg_user_id: int,
    *,
    email: str = "",
    password_enc: str = "",
    session_enc: str = "",
    org_id: str = "",
    org_slug: str = "",
    pager_locale: str = "",
    pager_user_id: str = "",
    session_ok: int = 0,
    last_error: str = "",
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM pager_accounts WHERE tg_user_id = ?", (tg_user_id,)
        )
        row = await cur.fetchone()
        if row:
            aid = row[0]
            await db.execute(
                """
                UPDATE pager_accounts SET
                    email = COALESCE(NULLIF(?, ''), email),
                    password_enc = COALESCE(NULLIF(?, ''), password_enc),
                    session_enc = COALESCE(NULLIF(?, ''), session_enc),
                    org_id = COALESCE(NULLIF(?, ''), org_id),
                    org_slug = COALESCE(NULLIF(?, ''), org_slug),
                    pager_locale = COALESCE(NULLIF(?, ''), pager_locale),
                    pager_user_id = COALESCE(NULLIF(?, ''), pager_user_id),
                    session_ok = ?,
                    last_error = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    email,
                    password_enc,
                    session_enc,
                    org_id,
                    org_slug,
                    pager_locale,
                    pager_user_id,
                    session_ok,
                    last_error,
                    aid,
                ),
            )
        else:
            cur = await db.execute(
                """
                INSERT INTO pager_accounts (
                    tg_user_id, email, password_enc, session_enc,
                    org_id, org_slug, pager_locale, pager_user_id, session_ok, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tg_user_id,
                    email,
                    password_enc,
                    session_enc,
                    org_id,
                    org_slug,
                    pager_locale or "uk",
                    pager_user_id,
                    session_ok,
                    last_error,
                ),
            )
            aid = cur.lastrowid
        await db.commit()
        return int(aid)


async def set_account_flags(
    tg_user_id: int,
    *,
    auto_reply: int | None = None,
    paused: int | None = None,
    escalation_chat_id: int | None = None,
) -> None:
    parts: list[str] = []
    vals: list[Any] = []
    if auto_reply is not None:
        parts.append("auto_reply = ?")
        vals.append(auto_reply)
    if paused is not None:
        parts.append("paused = ?")
        vals.append(paused)
    if escalation_chat_id is not None:
        parts.append("escalation_chat_id = ?")
        vals.append(escalation_chat_id)
    if not parts:
        return
    parts.append("updated_at = datetime('now')")
    vals.append(tg_user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE pager_accounts SET {', '.join(parts)} WHERE tg_user_id = ?",
            vals,
        )
        await db.commit()


async def delete_account(tg_user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pager_accounts WHERE tg_user_id = ?", (tg_user_id,))
        await db.commit()


async def list_worker_accounts() -> list[dict[str, Any]]:
    """One active worker row per Pager email (or per TG user if email empty)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT a.* FROM pager_accounts a
            INNER JOIN (
                SELECT COALESCE(NULLIF(email, ''), 'tg:' || tg_user_id) AS grp,
                       MAX(id) AS max_id
                FROM pager_accounts
                WHERE paused = 0 AND auto_reply = 1
                  AND email != '' AND password_enc != ''
                GROUP BY grp
            ) latest ON a.id = latest.max_id
            """
        )
        return [dict(r) for r in await cur.fetchall()]


async def deactivate_other_accounts(*, email: str = "", keep_id: int) -> None:
    """Disable duplicate Pager connections for the same email."""
    email = (email or "").strip()
    if not email:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE pager_accounts
            SET auto_reply = 0, session_ok = 0,
                last_error = 'Replaced by newer login',
                updated_at = datetime('now')
            WHERE email = ? AND id != ?
            """,
            (email, keep_id),
        )
        await db.commit()


async def sync_channels(
    account_id: int,
    channels: list[dict[str, str]],
    *,
    default_enabled: bool = False,
) -> None:
    """Upsert channels; keep enabled state for existing rows."""
    existing = {
        c["channel_id"]: int(c.get("enabled") or 0)
        for c in await list_channels(account_id)
    }
    incoming_ids = {ch["channel_id"] for ch in channels}

    async with aiosqlite.connect(DB_PATH) as db:
        for cid in set(existing) - incoming_ids:
            await db.execute(
                "DELETE FROM pager_channels WHERE account_id = ? AND channel_id = ?",
                (account_id, cid),
            )
        for ch in channels:
            cid = ch["channel_id"]
            name = ch.get("name", "")
            if cid in existing:
                await db.execute(
                    """
                    UPDATE pager_channels SET name = ?
                    WHERE account_id = ? AND channel_id = ?
                    """,
                    (name, account_id, cid),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO pager_channels (account_id, channel_id, name, enabled)
                    VALUES (?, ?, ?, ?)
                    """,
                    (account_id, cid, name, 1 if default_enabled else 0),
                )
        await db.commit()


async def replace_channels(account_id: int, channels: list[dict[str, str]]) -> None:
    """Legacy: full replace, all disabled by default."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pager_channels WHERE account_id = ?", (account_id,))
        await db.commit()
    await sync_channels(account_id, channels, default_enabled=False)


async def list_channels(account_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM pager_channels WHERE account_id = ? ORDER BY name",
            (account_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def enable_all_channels(account_id: int) -> int:
    """Turn on every channel for an account. Returns rows updated."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE pager_channels SET enabled = 1 WHERE account_id = ?",
            (account_id,),
        )
        await db.commit()
        return cur.rowcount or 0


async def toggle_channel(account_id: int, channel_id: str, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pager_channels SET enabled = ? WHERE account_id = ? AND channel_id = ?",
            (1 if enabled else 0, account_id, channel_id),
        )
        await db.commit()


async def clear_pauses_for_account(account_id: int) -> int:
    """Reset pauses and stale markers on paused chats."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE conversation_states
            SET pause_scripts = 0,
                human_takeover = 0,
                last_processed_msg_id = '',
                send_failures = 0,
                updated_at = datetime('now')
            WHERE account_id = ?
              AND (pause_scripts = 1 OR human_takeover = 1 OR send_failures > 0)
            """,
            (account_id,),
        )
        await db.commit()
        return cur.rowcount or 0


async def reset_conversation_states(account_id: int) -> int:
    """Delete all per-chat state (full queue reset)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM conversation_states WHERE account_id = ?",
            (account_id,),
        )
        await db.commit()
        return cur.rowcount or 0


async def get_conversation_state(account_id: int, conversation_id: str) -> dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM conversation_states WHERE account_id = ? AND conversation_id = ?",
            (account_id, conversation_id),
        )
        row = await cur.fetchone()
        if row:
            return dict(row)
    return {
        "account_id": account_id,
        "conversation_id": conversation_id,
        "step": 0,
        "human_takeover": 0,
        "pause_scripts": 0,
        "extracted_game_id": "",
        "last_processed_msg_id": "",
        "last_escalation_msg_id": "",
        "send_failures": 0,
    }


async def save_conversation_state(
    account_id: int,
    conversation_id: str,
    **fields: Any,
) -> None:
    st = await get_conversation_state(account_id, conversation_id)
    st.update(fields)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO conversation_states (
                account_id, conversation_id, step, human_takeover,
                pause_scripts, extracted_game_id, last_processed_msg_id,
                last_escalation_msg_id, send_failures, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(account_id, conversation_id) DO UPDATE SET
                step = excluded.step,
                human_takeover = excluded.human_takeover,
                pause_scripts = excluded.pause_scripts,
                extracted_game_id = excluded.extracted_game_id,
                last_processed_msg_id = excluded.last_processed_msg_id,
                last_escalation_msg_id = excluded.last_escalation_msg_id,
                send_failures = excluded.send_failures,
                updated_at = datetime('now')
            """,
            (
                account_id,
                conversation_id,
                st.get("step", 0),
                st.get("human_takeover", 0),
                st.get("pause_scripts", 0),
                st.get("extracted_game_id", ""),
                st.get("last_processed_msg_id", ""),
                st.get("last_escalation_msg_id", ""),
                int(st.get("send_failures") or 0),
            ),
        )
        await db.commit()


def session_cookies_from_encrypted(session_enc: str, secrets_decrypt) -> dict[str, str]:
    if not session_enc:
        return {}
    raw = secrets_decrypt(session_enc)
    data = json.loads(raw)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}
