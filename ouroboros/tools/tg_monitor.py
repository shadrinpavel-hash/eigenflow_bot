"""
Telegram Group Monitor — passive storage + LLM retrieval tools.

Architecture:
  - Messages are stored passively via store_group_message() called from supervisor
  - LLM uses search_group_messages(), get_group_summary(), list_monitored_groups()
    to retrieve context on demand
  - Storage: SQLite at Drive/memory/group_monitor.db (FTS5 for full-text search)

Privacy/Security notes:
  - Stores only message metadata + text (no media files)
  - Raw JSON capped at 4000 chars per message
  - Only messages from configured groups are stored
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import sqlite3
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB connection management
# ---------------------------------------------------------------------------

_DB_PATH:  Optional[pathlib.Path]    = None
_db_lock:  threading.Lock            = threading.Lock()
_db_conn:  Optional[sqlite3.Connection] = None


def _get_drive_root() -> pathlib.Path:
    env = os.environ.get("DRIVE_ROOT")
    if env:
        return pathlib.Path(env)
    return pathlib.Path("/content/drive/MyDrive/Ouroboros")


def _get_db_path() -> pathlib.Path:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = _get_drive_root() / "memory" / "group_monitor.db"
    return _DB_PATH


def _get_conn() -> sqlite3.Connection:
    global _db_conn
    with _db_lock:
        if _db_conn is None:
            path = _get_db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            _db_conn = sqlite3.connect(str(path), check_same_thread=False)
            _db_conn.row_factory = sqlite3.Row
            _init_schema(_db_conn)
        return _db_conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS monitored_groups (
            chat_id     INTEGER PRIMARY KEY,
            title       TEXT    NOT NULL,
            added_at    TEXT    NOT NULL,
            active      INTEGER DEFAULT 1,
            msg_count   INTEGER DEFAULT 0
        );

        -- Core message store
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            user_id     INTEGER,
            username    TEXT,
            first_name  TEXT,
            date_ts     INTEGER NOT NULL,
            date_utc    TEXT    NOT NULL,
            text        TEXT,
            has_media   INTEGER DEFAULT 0,
            media_type  TEXT,
            caption     TEXT,
            reply_to_id INTEGER,
            raw_json    TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(chat_id, message_id)
        );

        -- Per-sender stats per group
        CREATE TABLE IF NOT EXISTS group_members (
            user_id       INTEGER NOT NULL,
            chat_id       INTEGER NOT NULL,
            username      TEXT,
            first_name    TEXT,
            last_name     TEXT,
            last_seen_ts  INTEGER,
            message_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id)
        );

        -- Full-text search (FTS5) over message content
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text,
            caption,
            username,
            first_name,
            content='messages',
            content_rowid='id'
        );

        -- Trigger: keep FTS in sync on insert
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, text, caption, username, first_name)
            VALUES (new.id, new.text, new.caption, new.username, new.first_name);
        END;

        -- Performance indexes
        CREATE INDEX IF NOT EXISTS idx_msg_chat_date ON messages(chat_id, date_ts);
        CREATE INDEX IF NOT EXISTS idx_msg_user      ON messages(user_id);
        CREATE INDEX IF NOT EXISTS idx_msg_date      ON messages(date_ts);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Storage (called from supervisor message routing)
# ---------------------------------------------------------------------------

def _detect_media_type(msg: Dict[str, Any]) -> Optional[str]:
    for mtype in ("photo", "video", "document", "voice", "audio", "sticker", "animation"):
        if msg.get(mtype):
            return mtype
    return None


def store_group_message(msg: Dict[str, Any]) -> bool:
    """
    Persist a Telegram Bot API message dict to SQLite.

    Called from supervisor when a group message arrives.
    Returns True on success, False on error (never raises).
    """
    try:
        conn  = _get_conn()
        chat  = msg.get("chat") or {}
        sender = msg.get("from") or {}

        chat_id    = chat.get("id")
        message_id = msg.get("message_id")
        if not chat_id or not message_id:
            return False

        date_ts  = int(msg.get("date") or 0)
        date_utc = (
            datetime.datetime.utcfromtimestamp(date_ts).isoformat()
            if date_ts else ""
        )
        text       = msg.get("text") or msg.get("caption") or ""
        caption    = msg.get("caption")
        media_type = _detect_media_type(msg)
        has_media  = 1 if media_type else 0
        reply_to   = (msg.get("reply_to_message") or {}).get("message_id")
        raw        = json.dumps(msg, ensure_ascii=False)[:4000]

        with _db_lock:
            # Upsert group record
            conn.execute("""
                INSERT OR IGNORE INTO monitored_groups (chat_id, title, added_at, msg_count)
                VALUES (?, ?, ?, 0)
            """, (chat_id, chat.get("title", ""), datetime.datetime.utcnow().isoformat()))
            conn.execute("""
                UPDATE monitored_groups
                SET msg_count = msg_count + 1, active = 1, title = ?
                WHERE chat_id = ?
            """, (chat.get("title", ""), chat_id))

            # Insert message (silently skip duplicates)
            conn.execute("""
                INSERT OR IGNORE INTO messages
                  (chat_id, message_id, user_id, username, first_name,
                   date_ts, date_utc, text, has_media, media_type,
                   caption, reply_to_id, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                chat_id, message_id,
                sender.get("id"), sender.get("username"), sender.get("first_name"),
                date_ts, date_utc,
                text, has_media, media_type, caption, reply_to, raw,
            ))

            # Upsert sender stats
            if sender.get("id"):
                conn.execute("""
                    INSERT INTO group_members
                      (user_id, chat_id, username, first_name, last_name,
                       last_seen_ts, message_count)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(user_id, chat_id) DO UPDATE SET
                        username      = excluded.username,
                        first_name    = excluded.first_name,
                        last_seen_ts  = excluded.last_seen_ts,
                        message_count = message_count + 1
                """, (
                    sender["id"], chat_id,
                    sender.get("username"), sender.get("first_name"), sender.get("last_name"),
                    date_ts,
                ))

            conn.commit()
        return True

    except Exception as exc:
        log.warning("store_group_message failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# LLM tool: search
# ---------------------------------------------------------------------------

def search_group_messages(
    query: str,
    group_title: Optional[str] = None,
    days_back: int = 30,
    limit: int = 20,
) -> str:
    """
    Full-text search across monitored group messages.
    Returns plain-text results suitable for LLM context.
    """
    try:
        conn   = _get_conn()
        limit  = min(max(int(limit or 20), 1), 100)
        cutoff = int(
            (datetime.datetime.utcnow() - datetime.timedelta(days=int(days_back or 30))).timestamp()
        )

        params: List[Any] = [query, cutoff]
        group_join = group_cond = ""

        if group_title:
            group_join = "JOIN monitored_groups gf ON m.chat_id = gf.chat_id"
            group_cond = "AND gf.title LIKE ?"
            params.append(f"%{group_title}%")

        params.append(limit)

        sql = f"""
            SELECT m.date_utc, m.first_name, m.username, m.text,
                   m.caption, m.media_type,
                   g.title AS group_title
            FROM   messages_fts fts
            JOIN   messages          m ON fts.rowid = m.id
            LEFT   JOIN monitored_groups g ON m.chat_id = g.chat_id
            {group_join}
            WHERE  messages_fts MATCH ?
              AND  m.date_ts >= ?
              {group_cond}
            ORDER  BY m.date_ts DESC
            LIMIT  ?
        """
        rows = conn.execute(sql, params).fetchall()

        if not rows:
            ctx = f" in group '{group_title}'" if group_title else ""
            return f"No messages found matching '{query}'{ctx} in the last {days_back} days."

        out = [f"Found {len(rows)} message(s) matching '{query}':\n"]
        for r in rows:
            dt    = str(r["date_utc"] or "")[:16].replace("T", " ")
            name  = r["first_name"] or r["username"] or "Unknown"
            if r["username"]:
                name = f"{name} (@{r['username']})"
            grp   = r["group_title"] or "?"
            text  = (r["text"] or r["caption"] or f"[{r['media_type'] or 'media'}]")[:300]
            text  = text.replace("\n", " ")
            out.append(f"[{dt}] {grp} | {name}: {text}")

        return "\n".join(out)

    except Exception as exc:
        log.error("search_group_messages failed: %s", exc)
        return f"Search error: {exc}"


# ---------------------------------------------------------------------------
# LLM tool: list groups
# ---------------------------------------------------------------------------

def list_monitored_groups() -> str:
    """List all Telegram groups being monitored with message counts."""
    try:
        conn = _get_conn()
        rows = conn.execute("""
            SELECT title, chat_id, msg_count, added_at, active
            FROM   monitored_groups
            ORDER  BY msg_count DESC
        """).fetchall()

        if not rows:
            return (
                "No groups monitored yet.\n"
                "To start: add the bot to a group and disable privacy mode via BotFather "
                "(/setprivacy → Disable)."
            )

        lines = [f"Monitored Telegram groups ({len(rows)}):\n"]
        for r in rows:
            status = "✅" if r["active"] else "⏸️"
            since  = str(r["added_at"] or "")[:10]
            lines.append(
                f"  {status} [{r['chat_id']}] {r['title']} "
                f"— {r['msg_count']} messages (since {since})"
            )
        return "\n".join(lines)

    except Exception as exc:
        return f"Error listing groups: {exc}"


# ---------------------------------------------------------------------------
# LLM tool: group summary
# ---------------------------------------------------------------------------

def get_group_summary(group_title: str, hours_back: int = 24) -> str:
    """Get a chronological log of recent messages from a monitored group."""
    try:
        conn   = _get_conn()
        cutoff = int(
            (datetime.datetime.utcnow() - datetime.timedelta(hours=int(hours_back or 24))).timestamp()
        )

        group = conn.execute("""
            SELECT chat_id, title, msg_count FROM monitored_groups
            WHERE  title LIKE ? AND active = 1
            ORDER  BY msg_count DESC LIMIT 1
        """, (f"%{group_title}%",)).fetchone()

        if not group:
            return (
                f"Group '{group_title}' not found in monitored groups.\n"
                f"Available: {list_monitored_groups()}"
            )

        rows = conn.execute("""
            SELECT date_utc, first_name, username, text, caption, media_type
            FROM   messages
            WHERE  chat_id = ? AND date_ts >= ?
            ORDER  BY date_ts ASC
        """, (group["chat_id"], cutoff)).fetchall()

        if not rows:
            return f"No messages in '{group['title']}' in the last {hours_back}h."

        lines = [f"'{group['title']}' — last {hours_back}h ({len(rows)} messages):\n"]
        for r in rows:
            dt   = str(r["date_utc"] or "")[:16].replace("T", " ")
            name = r["first_name"] or r["username"] or "?"
            text = (r["text"] or r["caption"] or f"[{r['media_type'] or 'media'}]")[:200]
            text = text.replace("\n", " ")
            lines.append(f"[{dt}] {name}: {text}")

        return "\n".join(lines)

    except Exception as exc:
        return f"Error getting summary: {exc}"


# ---------------------------------------------------------------------------
# Tool registry (auto-discovered by ouroboros.tools loader)
# ---------------------------------------------------------------------------

def get_tools() -> List[Dict[str, Any]]:
    """Return LLM tool definitions for the Ouroboros tool registry."""
    return [
        {
            "name": "search_group_messages",
            "description": (
                "Full-text search across messages from monitored Telegram groups. "
                "Use when owner asks about group discussions, past decisions, contractor "
                "communications, or wants to find something specific in chat history."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to search (Russian or English)",
                    },
                    "group_title": {
                        "type": "string",
                        "description": "Optional: filter by group name (partial match)",
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "How many days back to search (default: 30)",
                        "default": 30,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 20, max: 100)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            "fn": search_group_messages,
        },
        {
            "name": "list_monitored_groups",
            "description": (
                "List all Telegram groups being monitored, with message counts and status."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
            "fn": list_monitored_groups,
        },
        {
            "name": "get_group_summary",
            "description": (
                "Get a chronological log of recent messages from a monitored Telegram group. "
                "Useful for catching up on what was discussed while owner was away."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "group_title": {
                        "type": "string",
                        "description": "Group name (partial match ok)",
                    },
                    "hours_back": {
                        "type": "integer",
                        "description": "How many hours back to show (default: 24)",
                        "default": 24,
                    },
                },
                "required": ["group_title"],
            },
            "fn": get_group_summary,
        },
    ]
