"""Yandex Mail IMAP tools — read inbox and search mail."""

import imaplib
import email
import os
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional


def _get_credentials():
    email_addr = os.environ.get("YANDEX_EMAIL", "")
    password = os.environ.get("YANDEX_APP_PASSWORD", "")
    return email_addr, password


def _decode_str(value) -> str:
    """Decode email header string (may be encoded)."""
    if value is None:
        return ""
    parts = decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except Exception:
                result.append(part.decode("latin-1", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def _get_body(msg) -> str:
    """Extract plain text body (first ~500 chars)."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
    return body[:500].strip()


def _connect():
    """Connect and login to Yandex IMAP."""
    email_addr, password = _get_credentials()
    if not email_addr or not password:
        raise RuntimeError(
            "YANDEX_EMAIL or YANDEX_APP_PASSWORD not set in environment"
        )
    conn = imaplib.IMAP4_SSL("imap.yandex.ru", 993)
    conn.login(email_addr, password)
    return conn


def _fetch_messages(conn, uids: list) -> list:
    """Fetch and parse messages by UID list."""
    results = []
    if not uids:
        return results
    uid_str = b",".join(uids)
    status, data = conn.uid("fetch", uid_str, "(RFC822)")
    if status != "OK":
        return results
    for response in data:
        if isinstance(response, tuple):
            msg = email.message_from_bytes(response[1])
            subject = _decode_str(msg.get("Subject", ""))
            from_ = _decode_str(msg.get("From", ""))
            date_raw = msg.get("Date", "")
            try:
                date_str = parsedate_to_datetime(date_raw).strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_str = date_raw
            body = _get_body(msg)
            results.append({
                "from": from_,
                "subject": subject,
                "date": date_str,
                "preview": body,
            })
    return results


def yandex_read_inbox(count: int = 10) -> str:
    """Read the latest N messages from Yandex Mail inbox.

    Args:
        count: Number of messages to fetch (default 10, max 50).

    Returns:
        Formatted list of messages with sender, subject, date, and preview.
    """
    count = min(int(count), 50)
    try:
        conn = _connect()
        conn.select("INBOX")
        status, data = conn.uid("search", None, "ALL")
        if status != "OK" or not data[0]:
            conn.logout()
            return "Inbox is empty or could not be read."
        all_uids = data[0].split()
        # Take the last N
        uids = all_uids[-count:]
        uids.reverse()  # newest first
        messages = _fetch_messages(conn, uids)
        conn.logout()

        if not messages:
            return "No messages found."

        lines = [f"📬 Last {len(messages)} messages from inbox:\n"]
        for i, m in enumerate(messages, 1):
            lines.append(
                f"{i}. [{m['date']}] {m['from']}\n"
                f"   Subject: {m['subject']}\n"
                f"   Preview: {m['preview'][:200]}\n"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading inbox: {e}"


def yandex_search_mail(
    query: str = "",
    from_addr: str = "",
    subject: str = "",
    since: str = "",
    limit: int = 20,
) -> str:
    """Search Yandex Mail by various criteria.

    Args:
        query: Free-text search in body/subject (IMAP TEXT search).
        from_addr: Filter by sender address/name.
        subject: Filter by subject keywords.
        since: Date filter in format 'DD-Mon-YYYY' (e.g. '01-Jan-2026').
        limit: Max results to return (default 20, max 50).

    Returns:
        Formatted list of matching messages.
    """
    limit = min(int(limit), 50)
    try:
        conn = _connect()
        conn.select("INBOX")

        # Build IMAP search criteria
        criteria = []
        if from_addr:
            criteria.append(f'FROM "{from_addr}"')
        if subject:
            criteria.append(f'SUBJECT "{subject}"')
        if since:
            criteria.append(f'SINCE {since}')
        if query:
            criteria.append(f'TEXT "{query}"')
        if not criteria:
            criteria.append("ALL")

        search_str = " ".join(criteria)
        status, data = conn.uid("search", None, search_str)
        if status != "OK" or not data[0]:
            conn.logout()
            return "No messages found matching your criteria."

        all_uids = data[0].split()
        # Most recent first
        uids = all_uids[-limit:]
        uids.reverse()

        messages = _fetch_messages(conn, uids)
        conn.logout()

        if not messages:
            return "No messages found."

        lines = [f"🔍 Found {len(messages)} message(s):\n"]
        for i, m in enumerate(messages, 1):
            lines.append(
                f"{i}. [{m['date']}] {m['from']}\n"
                f"   Subject: {m['subject']}\n"
                f"   Preview: {m['preview'][:300]}\n"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching mail: {e}"


def get_tools():
    return [
        {
            "function": yandex_read_inbox,
            "description": "Read the latest N messages from Yandex Mail inbox. Returns sender, subject, date, and message preview.",
            "parameters": {
                "count": {
                    "type": "integer",
                    "description": "Number of recent messages to fetch (default 10, max 50)",
                }
            },
        },
        {
            "function": yandex_search_mail,
            "description": "Search Yandex Mail by sender, subject, text, or date. Returns matching messages with preview.",
            "parameters": {
                "query": {
                    "type": "string",
                    "description": "Free-text search in body/subject",
                },
                "from_addr": {
                    "type": "string",
                    "description": "Filter by sender address or name",
                },
                "subject": {
                    "type": "string",
                    "description": "Filter by subject keywords",
                },
                "since": {
                    "type": "string",
                    "description": "Date filter in format DD-Mon-YYYY (e.g. 01-Jan-2026)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of results (default 20, max 50)",
                },
            },
        },
    ]
