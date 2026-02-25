"""Yandex Mail IMAP integration: read_inbox, search_mail.

Requires env vars:
  YANDEX_EMAIL       — full email address (login@yandex.ru)
  YANDEX_APP_PASSWORD — app password from Yandex ID → Security
"""

import email
import imaplib
import os
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional


IMAP_HOST = "imap.yandex.ru"
IMAP_PORT = 993


def _decode_str(value: str | bytes | None) -> str:
    """Decode RFC 2047 encoded header value to plain string."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        chunks, _ = decode_header(value.decode("utf-8", errors="replace"))[0]
        if isinstance(chunks, bytes):
            return chunks.decode("utf-8", errors="replace")
        return str(chunks)
    parts = decode_header(value)
    result = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(chunk)
    return "".join(result)


def _connect() -> imaplib.IMAP4_SSL:
    login = os.environ.get("YANDEX_EMAIL", "")
    password = os.environ.get("YANDEX_APP_PASSWORD", "")
    if not login or not password:
        raise ValueError(
            "YANDEX_EMAIL and YANDEX_APP_PASSWORD env vars must be set"
        )
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

    # Extract text body (prefer plain text, fallback to html snippet)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )

    # Truncate body for readability
    body_preview = body.strip()[:300].replace("\n", " ")

    return {
        "uid": uid.decode(),
        "date": date,
        "from": sender,
        "subject": subject,
        "preview": body_preview,
    }


def _format_messages(messages: list[dict]) -> str:
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


def read_inbox(count: int = 10, folder: str = "INBOX") -> str:
    """Read the latest N emails from Yandex Mail.

    Args:
        count: Number of latest messages to fetch (default: 10).
        folder: Mailbox folder name (default: INBOX).

    Returns:
        Formatted list of messages with date, sender, subject, body preview.
    """
    try:
        conn = _connect()
        conn.select(folder, readonly=True)
        _, uids_raw = conn.uid("search", None, "ALL")
        uids = uids_raw[0].split() if uids_raw and uids_raw[0] else []
        # Take last N
        uids = uids[-count:] if len(uids) > count else uids
        uids = list(reversed(uids))  # newest first

        messages = [_fetch_message(conn, uid) for uid in uids]
        conn.logout()
        return _format_messages(messages)
    except Exception as e:
        return f"Ошибка при чтении почты: {e}"


def search_mail(
    query: str,
    folder: str = "INBOX",
    max_results: int = 10,
    since: Optional[str] = None,
    sender: Optional[str] = None,
) -> str:
    """Search Yandex Mail using IMAP SEARCH criteria.

    Args:
        query: Text to search in subject and body (uses IMAP TEXT criterion).
        folder: Mailbox folder to search (default: INBOX).
        max_results: Maximum number of results to return (default: 10).
        since: Optional date filter, format DD-Mon-YYYY (e.g. '01-Jan-2026').
        sender: Optional sender email filter (partial match via FROM criterion).

    Returns:
        Formatted list of matching messages.
    """
    try:
        conn = _connect()
        conn.select(folder, readonly=True)

        # Build IMAP search criteria
        criteria = []
        if since:
            criteria += ["SINCE", since]
        if sender:
            criteria += ["FROM", sender]
        if query:
            criteria += ["TEXT", query]

        if not criteria:
            criteria = ["ALL"]

        # IMAP search requires charset for non-ASCII queries
        has_non_ascii = any(
            isinstance(c, str) and not c.isascii() for c in criteria
        )
        charset = "UTF-8" if has_non_ascii else None

        # Encode criteria for imaplib
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
        uids = list(reversed(uids))  # newest first
        uids = uids[:max_results]

        messages = [_fetch_message(conn, uid) for uid in uids]
        conn.logout()
        return _format_messages(messages)
    except Exception as e:
        return f"Ошибка при поиске по почте: {e}"


def get_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_inbox",
                "description": (
                    "Read the latest emails from Yandex Mail inbox. "
                    "Returns sender, date, subject, and body preview for each message."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "count": {
                            "type": "integer",
                            "description": "Number of latest messages to fetch (default: 10).",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Mailbox folder name (default: INBOX).",
                        },
                    },
                    "required": [],
                },
            },
            "fn": read_inbox,
        },
        {
            "type": "function",
            "function": {
                "name": "search_mail",
                "description": (
                    "Search Yandex Mail for messages matching a query. "
                    "Supports filtering by text, sender, and date."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Text to search in subject and body.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Mailbox folder to search (default: INBOX).",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 10).",
                        },
                        "since": {
                            "type": "string",
                            "description": "Date filter, format DD-Mon-YYYY (e.g. '01-Jan-2026').",
                        },
                        "sender": {
                            "type": "string",
                            "description": "Filter by sender email (partial match).",
                        },
                    },
                    "required": ["query"],
                },
            },
            "fn": search_mail,
        },
    ]
