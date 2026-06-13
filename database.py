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
                    org_id, pager_user_id, session_ok, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tg_user_id,
                    email,
                    password_enc,
                    session_enc,
                    org_id,
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM pager_accounts
            WHERE session_ok = 1 AND paused = 0 AND auto_reply = 1
            """
        )
        return [dict(r) for r in await cur.fetchall()]


async def replace_channels(account_id: int, channels: list[dict[str, str]]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pager_channels WHERE account_id = ?", (account_id,))
        for ch in channels:
            await db.execute(
                """
                INSERT INTO pager_channels (account_id, channel_id, name, enabled)
                VALUES (?, ?, ?, 1)
                """,
                (account_id, ch["channel_id"], ch.get("name", "")),
            )
        await db.commit()


async def list_channels(account_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM pager_channels WHERE account_id = ? ORDER BY name",
            (account_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def toggle_channel(account_id: int, channel_id: str, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pager_channels SET enabled = ? WHERE account_id = ? AND channel_id = ?",
            (1 if enabled else 0, account_id, channel_id),
        )
        await db.commit()


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
                pause_scripts, extracted_game_id, last_processed_msg_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(account_id, conversation_id) DO UPDATE SET
                step = excluded.step,
                human_takeover = excluded.human_takeover,
                pause_scripts = excluded.pause_scripts,
                extracted_game_id = excluded.extracted_game_id,
                last_processed_msg_id = excluded.last_processed_msg_id,
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
