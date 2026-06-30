"""SQLite persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from config import load_settings
from services.status_ids import ALL_INBOX_FOLDER_ID, NO_STATUS_FOLDER_ID

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
                enabled INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS pager_statuses (
                account_id INTEGER NOT NULL,
                status_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, status_id),
                FOREIGN KEY (account_id) REFERENCES pager_accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS pager_channel_folders (
                account_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                status_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, channel_id, status_id),
                FOREIGN KEY (account_id) REFERENCES pager_accounts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_channel_folders
                ON pager_channel_folders(account_id, channel_id);

            CREATE TABLE IF NOT EXISTS funnel_learn_success (
                account_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                geo TEXT NOT NULL DEFAULT '',
                game_id TEXT NOT NULL DEFAULT '',
                balance_text TEXT NOT NULL DEFAULT '',
                screenshot_kind TEXT NOT NULL DEFAULT '',
                client_name TEXT NOT NULL DEFAULT '',
                folder TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                learned_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (account_id, conversation_id, message_id),
                FOREIGN KEY (account_id) REFERENCES pager_accounts(id) ON DELETE CASCADE
            );
            """
        )
        for stmt in (
            "ALTER TABLE pager_accounts ADD COLUMN org_slug TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE pager_accounts ADD COLUMN pager_locale TEXT NOT NULL DEFAULT 'uk'",
            "ALTER TABLE conversation_states ADD COLUMN send_failures INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE conversation_states ADD COLUMN last_escalation_msg_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE pager_channels ADD COLUMN geo TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE funnel_learn_success ADD COLUMN dialog_text TEXT NOT NULL DEFAULT ''",
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
    geo: str | None = None,
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
    if geo is not None:
        parts.append("geo = ?")
        vals.append(geo.strip().lower() or "zm")
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


VALID_CHANNEL_GEOS = ("zm", "eg", "dj", "cm")


def normalize_channel_geo(geo: str, *, default: str = "zm") -> str:
    g = (geo or "").strip().lower()
    if g in VALID_CHANNEL_GEOS:
        return g
    d = (default or "zm").strip().lower()
    return d if d in VALID_CHANNEL_GEOS else "zm"


def next_channel_geo(current: str) -> str:
    order = list(VALID_CHANNEL_GEOS)
    cur = normalize_channel_geo(current, default=order[0])
    return order[(order.index(cur) + 1) % len(order)]


def infer_channel_geo_from_name(name: str, *, default: str = "zm") -> str:
    """Guess geo from Pager channel title (Ndzié/Brice → cm, Moses Zulu → dj, …)."""
    n = (name or "").lower()
    if any(
        x in n
        for x in (
            "ndzié",
            "ndzie",
            "mvondo",
            "brice",
            "moukoko",
            "cameroun",
            "cameroon",
            "камерун",
        )
    ):
        return "cm"
    if any(x in n for x in ("moses", "zulu", "djibouti")):
        return "dj"
    if any(x in n for x in ("mahmoud", "fathy", "egypt")):
        return "eg"
    return normalize_channel_geo(default)


def _should_apply_inferred_geo(stored: str, inferred: str, *, account_geo: str) -> bool:
    """Apply name-based geo when unset or clearly mis-tagged (e.g. Brice was dj → cm)."""
    if inferred == stored:
        return False
    if stored == account_geo:
        return inferred != account_geo
    if stored == "dj" and inferred == "cm":
        return True
    if stored == "zm" and inferred in ("cm", "dj", "eg"):
        return True
    return False


async def sync_channels(
    account_id: int,
    channels: list[dict[str, str]],
    *,
    default_enabled: bool = False,
) -> None:
    """Upsert channels; keep enabled state for existing rows."""
    existing_rows = await list_channels(account_id)
    existing = {
        c["channel_id"]: int(c.get("enabled") or 0) for c in existing_rows
    }
    existing_geos = {
        str(c["channel_id"]): normalize_channel_geo(
            str(c.get("geo") or ""), default="zm"
        )
        for c in existing_rows
    }
    incoming_ids = {ch["channel_id"] for ch in channels}

    account_geo = "zm"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT geo FROM pager_accounts WHERE id = ?", (account_id,)
        )
        row = await cur.fetchone()
        if row and row[0]:
            account_geo = normalize_channel_geo(str(row[0]))

        for cid in set(existing) - incoming_ids:
            await db.execute(
                "DELETE FROM pager_channels WHERE account_id = ? AND channel_id = ?",
                (account_id, cid),
            )
        for ch in channels:
            cid = ch["channel_id"]
            name = ch.get("name", "")
            inferred = infer_channel_geo_from_name(name, default=account_geo)
            if cid in existing:
                stored = existing_geos.get(cid, account_geo)
                geo = stored
                if _should_apply_inferred_geo(
                    stored, inferred, account_geo=account_geo
                ):
                    geo = inferred
                await db.execute(
                    """
                    UPDATE pager_channels SET name = ?, geo = ?
                    WHERE account_id = ? AND channel_id = ?
                    """,
                    (name, geo, account_id, cid),
                )
            else:
                ch_geo = normalize_channel_geo(
                    str(ch.get("geo") or ""), default=inferred
                )
                if ch_geo == account_geo and inferred != account_geo:
                    ch_geo = inferred
                await db.execute(
                    """
                    INSERT INTO pager_channels (account_id, channel_id, name, enabled, geo)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        cid,
                        name,
                        1 if default_enabled else 0,
                        ch_geo,
                    ),
                )
        await db.commit()


async def set_channel_geo(
    account_id: int, channel_id: str, geo: str
) -> str:
    """Set channel geo; returns normalized value stored."""
    g = normalize_channel_geo(geo)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE pager_channels SET geo = ?
            WHERE account_id = ? AND channel_id = ?
            """,
            (g, account_id, channel_id),
        )
        await db.commit()
    return g


async def get_channel_geo_map(
    account_id: int, *, account_geo: str = "zm"
) -> dict[str, str]:
    """channel_id -> geo (falls back to account default when channel geo unset)."""
    fallback = normalize_channel_geo(account_geo)
    out: dict[str, str] = {}
    for ch in await list_channels(account_id):
        cid = str(ch.get("channel_id") or "").strip()
        if not cid:
            continue
        raw = str(ch.get("geo") or "").strip().lower()
        out[cid] = normalize_channel_geo(raw, default=fallback) if raw else fallback
    return out


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


async def disable_all_channels(account_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE pager_channels SET enabled = 0 WHERE account_id = ?",
            (account_id,),
        )
        await db.commit()
        return cur.rowcount or 0


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


async def sync_statuses(
    account_id: int, statuses: list[dict[str, str]]
) -> None:
    """Upsert Pager folder list from GET /api/status."""
    async with aiosqlite.connect(DB_PATH) as db:
        for i, st in enumerate(statuses):
            sid = str(st.get("status_id") or "").strip()
            name = str(st.get("name") or sid).strip()
            if not sid:
                continue
            await db.execute(
                """
                INSERT INTO pager_statuses (account_id, status_id, name, sort_order)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, status_id) DO UPDATE SET
                    name = excluded.name,
                    sort_order = excluded.sort_order
                """,
                (account_id, sid, name, i),
            )
        await db.commit()


async def list_statuses(account_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT status_id, name, sort_order FROM pager_statuses
            WHERE account_id = ? ORDER BY sort_order, name
            """,
            (account_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


# Account-wide folder selection (not per Messenger channel).
ACCOUNT_FOLDER_SCOPE = "*"


async def has_account_folder_config(account_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT 1 FROM pager_channel_folders
            WHERE account_id = ? AND channel_id = ?
            LIMIT 1
            """,
            (account_id, ACCOUNT_FOLDER_SCOPE),
        )
        return await cur.fetchone() is not None


async def ensure_account_folder_defaults(account_id: int) -> None:
    await ensure_channel_folder_defaults(account_id, ACCOUNT_FOLDER_SCOPE)


async def list_account_folder_rows(account_id: int) -> list[dict[str, Any]]:
    return await list_channel_folder_rows(account_id, ACCOUNT_FOLDER_SCOPE)


async def toggle_account_folder(
    account_id: int, status_id: str, enabled: bool
) -> None:
    await toggle_channel_folder(account_id, ACCOUNT_FOLDER_SCOPE, status_id, enabled)


async def set_all_account_folders(account_id: int, enabled: bool) -> None:
    await set_all_channel_folders(account_id, ACCOUNT_FOLDER_SCOPE, enabled)


async def get_account_enabled_folders(account_id: int) -> set[str] | None:
    return await get_channel_enabled_folders(account_id, ACCOUNT_FOLDER_SCOPE)


async def has_folder_config(account_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM pager_channel_folders WHERE account_id = ? LIMIT 1",
            (account_id,),
        )
        return await cur.fetchone() is not None


async def ensure_channel_folder_defaults(account_id: int, channel_id: str) -> None:
    """First open: enable «Без статусу» only (not «Всі»)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT COUNT(*) FROM pager_channel_folders
            WHERE account_id = ? AND channel_id = ?
            """,
            (account_id, channel_id),
        )
        row = await cur.fetchone()
        if row and int(row[0] or 0) > 0:
            return
        await db.execute(
            """
            INSERT INTO pager_channel_folders (account_id, channel_id, status_id, enabled)
            VALUES (?, ?, ?, 1)
            """,
            (account_id, channel_id, NO_STATUS_FOLDER_ID),
        )
        await db.commit()


async def list_channel_folder_rows(
    account_id: int, channel_id: str
) -> list[dict[str, Any]]:
    """All folders for channel with enabled flags (includes «Без статусу»)."""
    await ensure_channel_folder_defaults(account_id, channel_id)
    statuses = await list_statuses(account_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT status_id, enabled FROM pager_channel_folders
            WHERE account_id = ? AND channel_id = ?
            """,
            (account_id, channel_id),
        )
        enabled_map = {
            str(r["status_id"]): int(r["enabled"] or 0)
            for r in await cur.fetchall()
        }
    rows: list[dict[str, Any]] = [
        {
            "status_id": ALL_INBOX_FOLDER_ID,
            "name": "Всі (все чаты)",
            "enabled": enabled_map.get(ALL_INBOX_FOLDER_ID, 0),
        },
        {
            "status_id": "",
            "name": "Без статусу",
            "enabled": enabled_map.get("", 0),
        },
    ]
    for st in statuses:
        sid = str(st["status_id"])
        rows.append(
            {
                "status_id": sid,
                "name": st.get("name") or sid[:8],
                "enabled": enabled_map.get(sid, 0),
            }
        )
    return rows


async def toggle_channel_folder(
    account_id: int, channel_id: str, status_id: str, enabled: bool
) -> None:
    sid = status_id if status_id is not None else ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO pager_channel_folders (account_id, channel_id, status_id, enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id, channel_id, status_id) DO UPDATE SET
                enabled = excluded.enabled
            """,
            (account_id, channel_id, sid, 1 if enabled else 0),
        )
        if enabled:
            if sid == ALL_INBOX_FOLDER_ID:
                await db.execute(
                    """
                    UPDATE pager_channel_folders
                    SET enabled = 0
                    WHERE account_id = ? AND channel_id = ?
                      AND status_id != ?
                    """,
                    (account_id, channel_id, ALL_INBOX_FOLDER_ID),
                )
            else:
                await db.execute(
                    """
                    UPDATE pager_channel_folders
                    SET enabled = 0
                    WHERE account_id = ? AND channel_id = ?
                      AND status_id = ?
                    """,
                    (account_id, channel_id, ALL_INBOX_FOLDER_ID),
                )
        await db.commit()


async def set_all_channel_folders(
    account_id: int, channel_id: str, enabled: bool
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if enabled:
            await db.execute(
                """
                UPDATE pager_channel_folders
                SET enabled = 0
                WHERE account_id = ? AND channel_id = ?
                """,
                (account_id, channel_id),
            )
            await db.execute(
                """
                INSERT INTO pager_channel_folders (account_id, channel_id, status_id, enabled)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(account_id, channel_id, status_id) DO UPDATE SET
                    enabled = 1
                """,
                (account_id, channel_id, ALL_INBOX_FOLDER_ID),
            )
        else:
            await db.execute(
                """
                UPDATE pager_channel_folders
                SET enabled = 0
                WHERE account_id = ? AND channel_id = ?
                """,
                (account_id, channel_id),
            )
        await db.commit()


async def get_channel_enabled_folders(
    account_id: int, channel_id: str
) -> set[str] | None:
    """Enabled folder ids for channel; None if never configured."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT status_id, enabled FROM pager_channel_folders
            WHERE account_id = ? AND channel_id = ?
            """,
            (account_id, channel_id),
        )
        rows = await cur.fetchall()
    if not rows:
        return None
    return {str(sid) for sid, en in rows if int(en or 0)}


async def build_channel_folders_map(
    account_id: int, enabled_channel_ids: set[str]
) -> dict[str, set[str] | None] | None:
    """Enabled status folders applied to all enabled channels."""
    await ensure_account_folder_defaults(account_id)
    account_folders = await get_account_enabled_folders(account_id)
    if account_folders is not None:
        return {cid: account_folders for cid in enabled_channel_ids}

    if not await has_folder_config(account_id):
        return None
    out: dict[str, set[str] | None] = {}
    for cid in enabled_channel_ids:
        out[cid] = await get_channel_enabled_folders(account_id, cid)
    return out


async def clear_pauses_for_account(account_id: int) -> int:
    """Reset pause flags only — keep escalation markers to avoid TG spam."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE conversation_states
            SET pause_scripts = 0,
                human_takeover = 0,
                send_failures = 0,
                updated_at = datetime('now')
            WHERE account_id = ?
              AND (pause_scripts = 1 OR human_takeover = 1 OR send_failures > 0)
              AND (last_escalation_msg_id IS NULL OR last_escalation_msg_id = '')
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


def default_conversation_state(
    account_id: int, conversation_id: str = ""
) -> dict[str, Any]:
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


async def load_conversation_states_map(
    account_id: int,
) -> dict[str, dict[str, Any]]:
    """One query per worker cycle instead of per-chat lookups."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM conversation_states WHERE account_id = ?",
            (account_id,),
        )
        rows = await cur.fetchall()
    return {str(row["conversation_id"]): dict(row) for row in rows}


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
    return default_conversation_state(account_id, conversation_id)


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


async def learn_success_exists(
    account_id: int, conversation_id: str, message_id: str
) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT 1 FROM funnel_learn_success
            WHERE account_id = ? AND conversation_id = ? AND message_id = ?
            """,
            (account_id, conversation_id, message_id),
        )
        return await cur.fetchone() is not None


async def save_learn_success(
    account_id: int,
    conversation_id: str,
    *,
    message_id: str,
    geo: str = "",
    game_id: str = "",
    balance_text: str = "",
    screenshot_kind: str = "",
    client_name: str = "",
    folder: str = "",
    note: str = "",
    dialog_text: str = "",
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO funnel_learn_success (
                account_id, conversation_id, message_id, geo, game_id,
                balance_text, screenshot_kind, client_name, folder, note,
                dialog_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, conversation_id, message_id) DO UPDATE SET
                geo = excluded.geo,
                game_id = excluded.game_id,
                balance_text = excluded.balance_text,
                screenshot_kind = excluded.screenshot_kind,
                client_name = excluded.client_name,
                folder = excluded.folder,
                note = excluded.note,
                dialog_text = excluded.dialog_text,
                learned_at = datetime('now')
            """,
            (
                account_id,
                conversation_id,
                message_id,
                (geo or "").strip(),
                (game_id or "").strip(),
                (balance_text or "").strip()[:64],
                (screenshot_kind or "").strip()[:32],
                (client_name or "").strip()[:64],
                (folder or "").strip()[:48],
                (note or "").strip()[:500],
                (dialog_text or "").strip()[:8000],
            ),
        )
        await db.commit()


async def count_learn_successes(account_id: int | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        if account_id is not None:
            cur = await db.execute(
                "SELECT COUNT(*) FROM funnel_learn_success WHERE account_id = ?",
                (account_id,),
            )
        else:
            cur = await db.execute("SELECT COUNT(*) FROM funnel_learn_success")
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def count_learn_successes_by_geo(
    account_id: int | None = None,
) -> dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        if account_id is not None:
            cur = await db.execute(
                """
                SELECT geo, COUNT(*) FROM funnel_learn_success
                WHERE account_id = ? AND geo != ''
                GROUP BY geo
                """,
                (account_id,),
            )
        else:
            cur = await db.execute(
                """
                SELECT geo, COUNT(*) FROM funnel_learn_success
                WHERE geo != ''
                GROUP BY geo
                """
            )
        rows = await cur.fetchall()
    return {str(g or "").strip().lower(): int(n) for g, n in rows if g}


async def list_learn_success_examples(
    geo: str, *, limit: int = 5, account_id: int | None = None
) -> list[dict[str, str]]:
    g = (geo or "").strip().lower()
    if not g:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if account_id is not None:
            cur = await db.execute(
                """
                SELECT geo, game_id, balance_text, screenshot_kind,
                       folder, client_name, note, dialog_text
                FROM funnel_learn_success
                WHERE geo = ? AND account_id = ?
                ORDER BY learned_at DESC
                LIMIT ?
                """,
                (g, account_id, max(1, limit)),
            )
        else:
            cur = await db.execute(
                """
                SELECT geo, game_id, balance_text, screenshot_kind,
                       folder, client_name, note, dialog_text
                FROM funnel_learn_success
                WHERE geo = ?
                ORDER BY learned_at DESC
                LIMIT ?
                """,
                (g, max(1, limit)),
            )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def count_learn_successes_by_folder(
    account_id: int | None = None,
) -> dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        if account_id is not None:
            cur = await db.execute(
                """
                SELECT folder, COUNT(*) FROM funnel_learn_success
                WHERE account_id = ? AND folder != ''
                GROUP BY folder
                ORDER BY COUNT(*) DESC
                """,
                (account_id,),
            )
        else:
            cur = await db.execute(
                """
                SELECT folder, COUNT(*) FROM funnel_learn_success
                WHERE folder != ''
                GROUP BY folder
                ORDER BY COUNT(*) DESC
                """
            )
        rows = await cur.fetchall()
    return {str(f or "").strip(): int(n) for f, n in rows if f}


async def list_learn_recent(
    account_id: int, *, limit: int = 10
) -> list[dict[str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT geo, game_id, balance_text, screenshot_kind,
                   folder, client_name, note, learned_at, dialog_text
            FROM funnel_learn_success
            WHERE account_id = ?
            ORDER BY learned_at DESC
            LIMIT ?
            """,
            (account_id, max(1, limit)),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


def session_cookies_from_encrypted(session_enc: str, secrets_decrypt) -> dict[str, str]:
    if not session_enc:
        return {}
    raw = secrets_decrypt(session_enc)
    data = json.loads(raw)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}
