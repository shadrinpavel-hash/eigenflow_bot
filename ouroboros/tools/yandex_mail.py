"""Yandex Mail IMAP tools — read inbox, search mail, and monitor important emails.

Tools:
  yandex_read_inbox        — fetch the latest N messages from INBOX
  yandex_search_mail       — search by sender, subject, text, date range
  yandex_check_credentials — verify secrets are available and test connection
  yandex_monitor_inbox    — quick triage feed with basic importance scoring

Credential resolution order (first non-empty wins):
  1. os.environ  — set by colab_launcher.py via userdata.get() before fork
  2. OUROBOROS_SESSION_ENV_FILE (default /tmp/ouroboros.env) —
     session env file written by launcher (subprocess fallback)
  3. google.colab.userdata — works only in direct Jupyter kernel context
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def _session_env_file_path() -> str:
    """Return path to launcher-written session env file."""
    return os.environ.get("OUROBOROS_SESSION_ENV_FILE", "/tmp/ouroboros.env")


def _load_env_file(path: str) -> dict:
    """Read key=value pairs from a file. Returns {} on error."""
    result = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" not in line or line.startswith("#"):
                    continue
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip()
    except Exception:
        pass
    return result


def _resolve_credentials() -> tuple[str, str, list[str]]:
    """Read YANDEX_EMAIL and YANDEX_APP_PASSWORD and return credential sources."""
    sources: list[str] = []
    email_addr = os.environ.get("YANDEX_EMAIL", "").strip()
    password = os.environ.get("YANDEX_APP_PASSWORD", "").strip()

    if email_addr or password:
        sources.append("os.environ")

    if not email_addr or not password:
        # Fallback 1: launcher-written env file (default /tmp/ouroboros.env)
        session_env_path = _session_env_file_path()
        env = _load_env_file(session_env_path)
        if (not email_addr and env.get("YANDEX_EMAIL")) or (not password and env.get("YANDEX_APP_PASSWORD")):
            sources.append(session_env_path)
        if not email_addr and env.get("YANDEX_EMAIL"):
            email_addr = env["YANDEX_EMAIL"]
            os.environ["YANDEX_EMAIL"] = email_addr
        if not password and env.get("YANDEX_APP_PASSWORD"):
            password = env["YANDEX_APP_PASSWORD"]
            os.environ["YANDEX_APP_PASSWORD"] = password

    if not email_addr or not password:
        # Fallback 2: google.colab.userdata (works only inside Jupyter kernel)
        try:
            from google.colab import userdata  # type: ignore

            colab_hit = False
            if not email_addr:
                v = (userdata.get("YANDEX_EMAIL") or "").strip()
                if v:
                    email_addr = v
                    os.environ["YANDEX_EMAIL"] = v
                    colab_hit = True
            if not password:
                v = (userdata.get("YANDEX_APP_PASSWORD") or "").strip()
                if v:
                    password = v
                    os.environ["YANDEX_APP_PASSWORD"] = v
                    colab_hit = True
            if colab_hit:
                sources.append("google.colab.userdata")
        except Exception:
            pass

    # Preserve order while removing duplicates
    unique_sources = list(dict.fromkeys(sources))
    return email_addr, password, unique_sources


def _get_credentials() -> tuple[str, str]:
    """Read YANDEX_EMAIL and YANDEX_APP_PASSWORD."""
    email_addr, password, _ = _resolve_credentials()
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
            ctype = part.get_content_type()
            if ctype != "text/plain":
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if part.get_filename():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except Exception:
                body = payload.decode("utf-8", errors="replace")
            if body.strip():
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
    return body[:1000].strip()


def _is_likely_important(message: dict) -> tuple[bool, list[str]]:
    """Heuristic scoring for important/urgent emails.

    This is intentionally lightweight (no LLM call) so it can run every cycle.
    """
    reasons: list[str] = []
    bucket = " ".join(
        [
            str(message.get("subject", "")),
            str(message.get("preview", "")),
            str(message.get("from", "")),
        ]
    ).lower()

    urgent_patterns = [
        r"\burgent\b",
        r"\basap\b",
        r"\bсрочно\b",
        r"\bважно\b",
        r"deadline",
        r"до\s+\d{1,2}[:.]\d{2}",
        r"до\s+\d{1,2}\s+[а-яa-z]+",
    ]
    business_patterns = [
        r"invoice|оплат|счет|счёт|payment",
        r"contract|договор|agreement",
        r"security|password|парол|2fa|код подтверждения",
        r"interview|собеседован|offer",
    ]

    if any(re.search(p, bucket, flags=re.IGNORECASE) for p in urgent_patterns):
        reasons.append("обнаружены маркеры срочности")
    if any(re.search(p, bucket, flags=re.IGNORECASE) for p in business_patterns):
        reasons.append("похоже на деловую/критичную тему")

    sender = str(message.get("from", "")).lower()
    if any(k in sender for k in ("noreply@", "no-reply@", "mailer-daemon")):
        reasons.append("системный отправитель")

    important = len(reasons) >= 1 and "системный отправитель" not in reasons
    return important, reasons


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


def _format_monitor_messages(messages: list) -> str:
    """Format triage output with importance flags and short rationale."""
    lines = ["📡 Мониторинг INBOX (новые сверху):", ""]
    important_total = 0
    for i, message in enumerate(messages, 1):
        important, reasons = _is_likely_important(message)
        if important:
            important_total += 1
        flag = "🔴 ВАЖНО" if important else "🟢"
        lines.append(f"{i}. {flag} [{message['date']}] {message['from']}")
        lines.append(f"   Тема: {message['subject']}")
        if reasons and important:
            lines.append(f"   Почему: {', '.join(reasons[:2])}")
        if message["preview"]:
            lines.append(f"   Превью: {message['preview'][:240]}")
        lines.append("")

    lines.append(f"Итого важных писем: {important_total} из {len(messages)}")
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
            header=f"📬 Последние {len(messages)} писем из входящих:",
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
    email_addr, password, sources = _resolve_credentials()

    if not email_addr:
        return (
            "❌ YANDEX_EMAIL не найден.\n"
            "Добавь в Colab Secrets с включённым Notebook Access, затем перезапусти агента."
        )
    if not password:
        return f"❌ YANDEX_APP_PASSWORD не найден. Email найден: {email_addr}"

    source_info = f" (из {', '.join(sources)})" if sources else ""
    try:
        conn = _connect()
        conn.select("INBOX")
        status, data = conn.uid("search", None, "ALL")
        total = len(data[0].split()) if status == "OK" and data[0] else 0
        conn.logout()
        return (
            f"✅ Подключение успешно{source_info}.\n"
            f"Email: {email_addr}\n"
            f"Писем в INBOX: {total}"
        )
    except Exception as e:
        return f"❌ Ошибка подключения к imap.yandex.ru{source_info}: {e}"


def _yandex_monitor_inbox(
    ctx: ToolContext,
    limit: int = 20,
    unseen_only: bool = True,
) -> str:
    """Quick mailbox monitoring feed with basic importance triage.

    Args:
        limit: Number of latest emails to inspect (1..50).
        unseen_only: If true, inspect only unread messages.
    """
    limit = max(1, min(int(limit), 50))

    try:
        conn = _connect()
        conn.select("INBOX")
        query = "UNSEEN" if unseen_only else "ALL"
        status, data = conn.uid("search", None, query)
        if status != "OK" or not data[0]:
            conn.logout()
            mode = "непрочитанных" if unseen_only else "сообщений"
            return f"Писем не найдено ({mode})."

        all_uids = data[0].split()
        uids = list(reversed(all_uids[-limit:]))
        messages = _fetch_messages(conn, uids)
        conn.logout()

        if not messages:
            return "Писем не найдено."
        return _format_monitor_messages(messages)
    except Exception as e:
        return f"Ошибка мониторинга почты: {e}"


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
                    "are available and test the IMAP connection. Use this to diagnose issues."
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
        ToolEntry(
            name="yandex_monitor_inbox",
            schema={
                "name": "yandex_monitor_inbox",
                "description": (
                    "Monitor Yandex inbox (preferably unread emails) and return a triage feed "
                    "with likely-important messages highlighted for fast reaction."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "How many latest messages to inspect (default 20, max 50)",
                        },
                        "unseen_only": {
                            "type": "boolean",
                            "description": "If true, inspect only unread messages (default true)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_yandex_monitor_inbox,
            timeout_sec=30,
        ),
    ]
