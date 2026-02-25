"""Yandex Mail IMAP tools — read inbox and search mail.

Tools:
  yandex_read_inbox  — fetch the latest N messages from INBOX
  yandex_search_mail — search by sender, subject, text, date range

Credentials are read from os.environ (YANDEX_EMAIL, YANDEX_APP_PASSWORD).
Fallback: /tmp/ouroboros.env (session-local file written by launcher).
"""

from __future__ import annotations

import email
import imaplib
import os
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TMP_ENV_FILE = "/tmp/ouroboros.env"


def _load_tmp_env() -> dict:
    """Read KEY=VALUE pairs from /tmp/ouroboros.env (session-local, not on Drive)."""
    result: dict = {}
    try:
        if os.path.exists(_TMP_ENV_FILE):
            with open(_TMP_ENV_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        result[k.strip()] = v.strip()
    except Exception:
        pass
    return result


def _get_credentials() -> tuple[str, str]:
    email_addr = os.environ.get("YANDEX_EMAIL", "")
    password = os.environ.get("YANDEX_APP_PASSWORD", "")

    # Fallback: read from session-local env file written by launcher
    if not email_addr or not password:
        tmp_env = _load_tmp_env()
        if not email_addr:
            email_addr = tmp_env.get("YANDEX_EMAIL", "")
        if not password:
            password = tmp_env.get("YANDEX_APP_PASSWORD", "")

    return email_addr, password


def _decode_str(value) -> str:
    """Decode possibly-encoded email header value."""
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
    """Extract plain text body (first 500 chars)."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
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


def _connect() -> imaplib.IMAP4_SSL:
    """Establish authenticated IMAP connection to Yandex."""
    email_addr, password = _get_credentials()
    if not email_addr or not password:
        raise RuntimeError(
            "YANDEX_EMAIL or YANDEX_APP_PASSWORD not set.\n"
            "Run this once in a Colab cell before starting the agent:\n\n"
            "  from google.colab import userdata\n"
            "  with open('/tmp/ouroboros.env', 'w') as f:\n"
            "      f.write(f\"YANDEX_EMAIL={userdata.get('YANDEX_EMAIL')}\\n\")\n"
            "      f.write(f\"YANDEX_APP_PASSWORD={userdata.get('YANDEX_APP_PASSWORD')}\\n\")\n"
            "  print('Done')"
        )
    conn = imaplib.IMAP4_SSL("imap.yandex.ru", 993)
    conn.login(email_addr, password)
    return conn


def _fetch_messages(conn: imaplib.IMAP4_SSL, uids: list) -> list:
    """Fetch and parse messages by UID list. Returns list of dicts."""
    if not uids:
        return []
    uid_str = b",".join(uids)
    status, data = conn.uid("fetch", uid_str, "(RFC822)")
    if status != "OK":
        return []
    results = []
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


def _format_messages(messages: list, header: str, preview_len: int = 250) -> str:
    """Format a list of message dicts into readable text."""
    lines = [header, ""]
    for i, m in enumerate(messages, 1):
        lines.append(f"{i}. [{m['date']}] {m['from']}")
        lines.append(f"   Тема: {m['subject']}")
        if m["preview"]:
            lines.append(f"   Превью: {m['preview'][:preview_len]}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _yandex_read_inbox(ctx: ToolContext, count: int = 10) -> str:
    """Read the latest N messages from Yandex Mail inbox."""
    count = max(1, min(int(count), 50))
    try:
        conn = _connect()
        conn.select("INBOX")
        status, data = conn.uid("search", None, "ALL")
        if status != "OK" or not data[0]:
            conn.logout()
            return "Входящие пусты или недоступны."
        all_uids = data[0].split()
        uids = list(reversed(all_uids[-count:]))  # newest first
        messages = _fetch_messages(conn, uids)
        conn.logout()
        if not messages:
            return "Писем не найдено."
        return _format_messages(
            messages,
            header=f"📬 Последние {len(messages)} писем из входящих:"
        )
    except Exception as e:
        return f"Ошибка чтения почты: {e}"


def _yandex_search_mail(
    ctx: ToolContext,
    query: str = "",
    from_addr: str = "",
    subject: str = "",
    since: str = "",
    limit: int = 20,
) -> str:
    """Search Yandex Mail by sender, subject, text, or date."""
    limit = max(1, min(int(limit), 50))
    try:
        conn = _connect()
        conn.select("INBOX")

        # Build IMAP search criteria
        criteria: list[str] = []
        if from_addr:
            criteria.append(f'FROM "{from_addr}"')
        if subject:
            criteria.append(f'SUBJECT "{subject}"')
        if since:
            # Expected format: DD-Mon-YYYY (e.g. 01-Jan-2026)
            criteria.append(f"SINCE {since}")
        if query:
            criteria.append(f'TEXT "{query}"')
        if not criteria:
            criteria.append("ALL")

        search_str = " ".join(criteria)
        status, data = conn.uid("search", None, search_str)
        if status != "OK" or not data[0]:
            conn.logout()
            return "Писем по вашим критериям не найдено."

        all_uids = data[0].split()
        uids = list(reversed(all_uids[-limit:]))  # newest first
        messages = _fetch_messages(conn, uids)
        conn.logout()

        if not messages:
            return "Писем не найдено."

        parts = []
        if from_addr:
            parts.append(f"от: {from_addr}")
        if subject:
            parts.append(f"тема: {subject}")
        if query:
            parts.append(f"текст: {query}")
        if since:
            parts.append(f"с {since}")
        criteria_str = ", ".join(parts) if parts else "все"

        return _format_messages(
            messages,
            header=f"🔍 Найдено {len(messages)} писем ({criteria_str}):",
            preview_len=300,
        )
    except Exception as e:
        return f"Ошибка поиска по почте: {e}"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="yandex_read_inbox",
            schema={
                "name": "yandex_read_inbox",
                "description": (
                    "Read the latest N messages from Yandex Mail inbox. "
                    "Returns sender, subject, date, and message preview for each."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "count": {
                            "type": "integer",
                            "description": "Number of recent messages to fetch (default 10, max 50)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_yandex_read_inbox,
            timeout_sec=30,
        ),
        ToolEntry(
            name="yandex_search_mail",
            schema={
                "name": "yandex_search_mail",
                "description": (
                    "Search Yandex Mail by sender, subject, free text, or date. "
                    "Returns matching messages with preview. All parameters are optional — "
                    "at least one filter is recommended."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Free-text search in body/subject (IMAP TEXT)",
                        },
                        "from_addr": {
                            "type": "string",
                            "description": "Filter by sender email address or name",
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
                    "required": [],
                },
            },
            handler=_yandex_search_mail,
            timeout_sec=30,
        ),
    ]
