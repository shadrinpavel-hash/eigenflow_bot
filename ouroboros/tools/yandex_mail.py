"""Yandex Mail IMAP tools — read inbox and search mail.

Tools:
  yandex_read_inbox      — fetch the latest N messages from INBOX
  yandex_search_mail     — search by sender, subject, text, date range
  yandex_check_credentials — verify credentials and test IMAP connection

Credential loading order (most to least preferred):
  1. os.environ — inherited from colab_launcher via fork()
  2. google.colab.userdata — works when called from Jupyter kernel context
  3. /tmp/ouroboros.env — session-local file written by colab_launcher at startup
  4. /content/drive/.../secrets.env — last resort (not written by default)
"""

from __future__ import annotations

import email
import imaplib
import os
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env_file(path: str) -> dict:
    """Parse a KEY=VALUE env file. Returns dict of found keys."""
    result = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip()
    except Exception:
        pass
    return result


def _get_credentials() -> tuple[str, str]:
    """Read YANDEX_EMAIL and YANDEX_APP_PASSWORD.

    Tries multiple sources in order, caching results in os.environ
    so subsequent calls are cheap.

    Priority:
    1. os.environ — fastest; set by colab_launcher and inherited via fork()
    2. google.colab.userdata — works when code runs in a Jupyter kernel cell
    3. /tmp/ouroboros.env — written by colab_launcher at startup for subprocess access
    4. Drive secrets.env — last resort (plaintext on Drive, not written by default)
    """
    email_addr = os.environ.get("YANDEX_EMAIL", "").strip()
    password = os.environ.get("YANDEX_APP_PASSWORD", "").strip()

    # --- Fallback 2: google.colab.userdata ---
    # Works when the call context is a Jupyter kernel (not subprocess workers).
    if not email_addr or not password:
        try:
            from google.colab import userdata  # type: ignore
            if not email_addr:
                email_addr = (userdata.get("YANDEX_EMAIL") or "").strip()
            if not password:
                password = (userdata.get("YANDEX_APP_PASSWORD") or "").strip()
            if email_addr:
                os.environ["YANDEX_EMAIL"] = email_addr
            if password:
                os.environ["YANDEX_APP_PASSWORD"] = password
        except Exception:
            pass

    # --- Fallback 3: /tmp/ouroboros.env ---
    # Written by colab_launcher.py at startup with the secrets it read from userdata.
    # This is the reliable path for subprocess workers that inherit env from fork().
    if not email_addr or not password:
        env_data = _load_env_file("/tmp/ouroboros.env")
        if not email_addr and "YANDEX_EMAIL" in env_data:
            email_addr = env_data["YANDEX_EMAIL"]
            os.environ["YANDEX_EMAIL"] = email_addr
        if not password and "YANDEX_APP_PASSWORD" in env_data:
            password = env_data["YANDEX_APP_PASSWORD"]
            os.environ["YANDEX_APP_PASSWORD"] = password

    # --- Fallback 4: secrets.env on Drive ---
    # Legacy / last resort. Not written by default (would be plaintext).
    if not email_addr or not password:
        env_data = _load_env_file("/content/drive/MyDrive/Ouroboros/secrets.env")
        if not email_addr and "YANDEX_EMAIL" in env_data:
            email_addr = env_data["YANDEX_EMAIL"]
            os.environ["YANDEX_EMAIL"] = email_addr
        if not password and "YANDEX_APP_PASSWORD" in env_data:
            password = env_data["YANDEX_APP_PASSWORD"]
            os.environ["YANDEX_APP_PASSWORD"] = password

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
    """Extract plain text body (first 1000 chars)."""
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
    return body[:1000].strip()


def _connect() -> imaplib.IMAP4_SSL:
    """Establish authenticated IMAP connection to Yandex."""
    email_addr, password = _get_credentials()
    if not email_addr or not password:
        raise RuntimeError(
            "YANDEX_EMAIL or YANDEX_APP_PASSWORD not available. "
            "Make sure they are added to Colab Secrets with Notebook Access enabled, "
            "then restart the agent."
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


def _format_messages(messages: list, header: str, preview_len: int = 300) -> str:
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
            preview_len=400,
        )
    except Exception as e:
        return f"Ошибка поиска по почте: {e}"


def _yandex_check_credentials(ctx: ToolContext) -> str:
    """Check if Yandex Mail credentials are available and test the IMAP connection."""
    email_addr, password = _get_credentials()

    # Show which sources are available for diagnostics
    sources = []
    if os.environ.get("YANDEX_EMAIL"):
        sources.append("os.environ")
    try:
        from google.colab import userdata  # type: ignore
        if (userdata.get("YANDEX_EMAIL") or "").strip():
            sources.append("colab.userdata")
    except Exception:
        pass
    import pathlib
    if pathlib.Path("/tmp/ouroboros.env").exists():
        env_data = _load_env_file("/tmp/ouroboros.env")
        if "YANDEX_EMAIL" in env_data:
            sources.append("/tmp/ouroboros.env")

    if not email_addr:
        return (
            f"❌ YANDEX_EMAIL не найден ни в одном источнике.\n"
            f"Добавь в Colab Secrets → включи Notebook Access → перезапусти агента."
        )
    if not password:
        return (
            f"❌ YANDEX_APP_PASSWORD не найден. Email найден: {email_addr}\n"
            f"Источники: {', '.join(sources) or 'нет'}"
        )

    try:
        conn = _connect()
        conn.select("INBOX")
        status, data = conn.uid("search", None, "ALL")
        total = len(data[0].split()) if status == "OK" and data[0] else 0
        conn.logout()
        return (
            f"✅ Подключение успешно.\n"
            f"Email: {email_addr}\n"
            f"Источник: {', '.join(sources) or 'неизвестен'}\n"
            f"Всего писем в INBOX: {total}."
        )
    except Exception as e:
        return (
            f"❌ Ошибка подключения к imap.yandex.ru: {e}\n"
            f"Email: {email_addr} | Источник: {', '.join(sources) or 'нет'}"
        )


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
        ToolEntry(
            name="yandex_check_credentials",
            schema={
                "name": "yandex_check_credentials",
                "description": (
                    "Check if Yandex Mail credentials (YANDEX_EMAIL and YANDEX_APP_PASSWORD) "
                    "are available and test the IMAP connection. Use this to diagnose credential issues."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            handler=_yandex_check_credentials,
            timeout_sec=15,
        ),
    ]
