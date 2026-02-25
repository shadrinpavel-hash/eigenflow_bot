"""Yandex Mail IMAP integration: read_inbox, search_mail.

Requires env vars:
  YANDEX_EMAIL       — full email address (login@yandex.ru)
  YANDEX_APP_PASSWORD — app password from Yandex ID → Security
"""

from __future__ import annotations

import email
import imaplib
import os
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry


IMAP_HOST = "imap.yandex.ru"
IMAP_PORT = 993


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decode_str(value) -> str:
    """Decode RFC 2047 encoded header value to plain string."""
    if value is None:
        return ""
    parts = decode_header(value if isinstance(value, str) else value.decode("utf-8", errors="replace"))
    result = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(chunk))
    return "".join(result)


def _connect() -> imaplib.IMAP4_SSL:
    login = os.environ.get("YANDEX_EMAIL", "")
    password = os.environ.get("YANDEX_APP_PASSWORD", "")
    if not login or not password:
        raise ValueError("YANDEX_EMAIL and YANDEX_APP_PASSWORD env vars must be set")
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(login, password)
    return conn


def _fetch_message(conn: imaplib.IMAP4_SSL, uid: bytes) -> dict:
    """Fetch and parse a single message by UID."""
    _, data = conn.uid("fetch", uid, "(RFC822)")
    raw = data[0][1] if data and data[0] else b""
    msg = email.message_from_bytes(raw)

    subject = _decode_str(msg.get("Subject", ""))
    sender = _decode_str(msg.get("From", ""))
    date_str = msg.get("Date", "")
    try:
        date = parsedate_to_datetime(date_str).strftime("%Y-%m-%d %H:%M")
    except Exception:
        date = date_str

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    return {
        "uid": uid.decode(),
        "date": date,
        "from": sender,
        "subject": subject,
        "preview": body.strip()[:300].replace("\n", " "),
    }


def _format_messages(messages: List[dict]) -> str:
    if not messages:
        return "Писем не найдено."
    lines = []
    for i, m in enumerate(messages, 1):
        lines.append(
            f"{i}. [{m['date']}] От: {m['from']}\n"
            f"   Тема: {m['subject']}\n"
            f"   {m['preview']}"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _read_inbox(ctx: ToolContext, count: int = 10, folder: str = "INBOX") -> str:
    try:
        conn = _connect()
        conn.select(folder, readonly=True)
        _, uids_raw = conn.uid("search", None, "ALL")
        uids = uids_raw[0].split() if uids_raw and uids_raw[0] else []
        uids = list(reversed(uids[-count:] if len(uids) > count else uids))
        messages = [_fetch_message(conn, uid) for uid in uids]
        conn.logout()
        return _format_messages(messages)
    except Exception as e:
        return f"Ошибка при чтении почты: {e}"


def _search_mail(
    ctx: ToolContext,
    query: str,
    folder: str = "INBOX",
    max_results: int = 10,
    since: Optional[str] = None,
    sender: Optional[str] = None,
) -> str:
    try:
        conn = _connect()
        conn.select(folder, readonly=True)

        criteria: list = []
        if since:
            criteria += ["SINCE", since]
        if sender:
            criteria += ["FROM", sender]
        if query:
            criteria += ["TEXT", query]
        if not criteria:
            criteria = ["ALL"]

        has_non_ascii = any(isinstance(c, str) and not c.isascii() for c in criteria)
        charset = "UTF-8" if has_non_ascii else None

        encoded = []
        for c in criteria:
            if isinstance(c, str) and not c.isascii():
                encoded.append(c.encode("utf-8"))
            else:
                encoded.append(c)

        if charset:
            _, uids_raw = conn.uid("search", f"CHARSET {charset}", *encoded)
        else:
            _, uids_raw = conn.uid("search", None, *encoded)

        uids = uids_raw[0].split() if uids_raw and uids_raw[0] else []
        uids = list(reversed(uids))[:max_results]

        messages = [_fetch_message(conn, uid) for uid in uids]
        conn.logout()
        return _format_messages(messages)
    except Exception as e:
        return f"Ошибка при поиске по почте: {e}"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="read_inbox",
            schema={
                "name": "read_inbox",
                "description": (
                    "Read the latest emails from Yandex Mail inbox. "
                    "Returns sender, date, subject, and body preview for each message."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "description": "Number of latest messages (default: 10)."},
                        "folder": {"type": "string", "description": "Mailbox folder (default: INBOX)."},
                    },
                    "required": [],
                },
            },
            handler=_read_inbox,
        ),
        ToolEntry(
            name="search_mail",
            schema={
                "name": "search_mail",
                "description": (
                    "Search Yandex Mail for messages matching a query. "
                    "Supports filtering by text, sender, and date range."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Text to search in subject and body."},
                        "folder": {"type": "string", "description": "Mailbox folder (default: INBOX)."},
                        "max_results": {"type": "integer", "description": "Max results (default: 10)."},
                        "since": {"type": "string", "description": "Date filter DD-Mon-YYYY (e.g. '01-Jan-2026')."},
                        "sender": {"type": "string", "description": "Filter by sender email (partial match)."},
                    },
                    "required": ["query"],
                },
            },
            handler=_search_mail,
        ),
    ]
