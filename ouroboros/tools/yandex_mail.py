"""Yandex Mail integration via IMAP: read_inbox and search_mail."""

from __future__ import annotations

import email
import email.header
import imaplib
import logging
import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

IMAP_HOST = "imap.yandex.ru"
IMAP_PORT = 993


def _get_credentials() -> tuple[str, str]:
    """Read credentials from environment variables."""
    login = os.environ.get("YANDEX_EMAIL", "")
    password = os.environ.get("YANDEX_APP_PASSWORD", "")
    if not login or not password:
        raise ValueError("YANDEX_EMAIL or YANDEX_APP_PASSWORD not set in environment")
    return login, password


def _connect() -> imaplib.IMAP4_SSL:
    login, password = _get_credentials()
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(login, password)
    return mail


def _decode_header_value(value: str) -> str:
    """Decode RFC 2047 encoded header value."""
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_text_body(msg: email.message.Message) -> str:
    """Extract plain text body from email message."""
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                charset = part.get_content_charset() or "utf-8"
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                body_parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass
    return "\n".join(body_parts).strip()


def _format_message(msg: email.message.Message, uid: str) -> Dict[str, Any]:
    """Format email message as a dict."""
    subject = _decode_header_value(msg.get("Subject", "(нет темы)"))
    sender = _decode_header_value(msg.get("From", ""))
    date_str = msg.get("Date", "")
    message_id = msg.get("Message-ID", "")

    # Parse date
    try:
        dt = parsedate_to_datetime(date_str)
        date_formatted = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        date_formatted = date_str

    body = _extract_text_body(msg)
    # Truncate long bodies for preview
    preview = body[:500] + "..." if len(body) > 500 else body

    return {
        "uid": uid,
        "subject": subject,
        "from": sender,
        "date": date_formatted,
        "preview": preview,
        "message_id": message_id,
    }


def _read_inbox(ctx: ToolContext, count: int = 10, folder: str = "INBOX") -> str:
    """Read recent emails from inbox."""
    try:
        mail = _connect()
        try:
            mail.select(folder)
            # Search all messages, get last N UIDs
            status, data = mail.uid("search", None, "ALL")
            if status != "OK":
                return f"⚠️ Failed to search mailbox: {status}"

            uids = data[0].split()
            if not uids:
                return "📭 Нет писем в папке."

            # Take last N
            recent_uids = uids[-count:]
            recent_uids.reverse()  # newest first

            messages = []
            for uid in recent_uids:
                status, msg_data = mail.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if isinstance(raw, bytes):
                    msg = email.message_from_bytes(raw)
                    messages.append(_format_message(msg, uid.decode()))

            if not messages:
                return "📭 Не удалось прочитать письма."

            # Format output
            lines = [f"📬 **{folder}** — последние {len(messages)} писем:\n"]
            for i, m in enumerate(messages, 1):
                lines.append(f"**{i}. {m['subject']}**")
                lines.append(f"   От: {m['from']}")
                lines.append(f"   Дата: {m['date']}")
                if m["preview"]:
                    preview_lines = m["preview"].replace("\r\n", "\n").replace("\r", "\n").split("\n")
                    short = " ".join(l.strip() for l in preview_lines[:3] if l.strip())
                    if short:
                        lines.append(f"   Текст: {short[:200]}...")
                lines.append("")

            return "\n".join(lines)

        finally:
            try:
                mail.logout()
            except Exception:
                pass

    except ValueError as e:
        return f"⚠️ {e}"
    except imaplib.IMAP4.error as e:
        return f"⚠️ IMAP ошибка: {e}"
    except Exception as e:
        log.warning("read_inbox failed", exc_info=True)
        return f"⚠️ Ошибка: {repr(e)}"


def _search_mail(
    ctx: ToolContext,
    query: str = "",
    sender: str = "",
    subject: str = "",
    since: str = "",
    folder: str = "INBOX",
    limit: int = 20,
) -> str:
    """Search emails by various criteria."""
    try:
        mail = _connect()
        try:
            mail.select(folder)

            # Build IMAP search criteria
            criteria = []

            if sender:
                criteria.append(f'FROM "{sender}"')

            if subject or query:
                search_term = subject or query
                criteria.append(f'SUBJECT "{search_term}"')

            if since:
                try:
                    dt = datetime.strptime(since, "%Y-%m-%d")
                    imap_date = dt.strftime("%d-%b-%Y")
                    criteria.append(f"SINCE {imap_date}")
                except ValueError:
                    pass

            if not criteria:
                criteria = ["ALL"]

            search_str = " ".join(criteria)
            status, data = mail.uid("search", None, search_str)

            if status != "OK":
                return f"⚠️ Поиск не удался: {status}"

            uids = data[0].split()
            if not uids:
                return f"🔍 Ничего не найдено по запросу: {search_str}"

            # Take last `limit` results, newest first
            selected = uids[-limit:]
            selected.reverse()

            messages = []
            for uid in selected:
                status, msg_data = mail.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if isinstance(raw, bytes):
                    msg = email.message_from_bytes(raw)
                    messages.append(_format_message(msg, uid.decode()))

            if not messages:
                return "🔍 Не удалось получить найденные письма."

            lines = [f"🔍 Найдено {len(uids)} писем, показаны последние {len(messages)}:\n"]
            for i, m in enumerate(messages, 1):
                lines.append(f"**{i}. {m['subject']}**")
                lines.append(f"   От: {m['from']}")
                lines.append(f"   Дата: {m['date']}")
                if m["preview"]:
                    preview_lines = m["preview"].replace("\r\n", "\n").replace("\r", "\n").split("\n")
                    short = " ".join(l.strip() for l in preview_lines[:3] if l.strip())
                    if short:
                        lines.append(f"   Текст: {short[:200]}...")
                lines.append("")

            return "\n".join(lines)

        finally:
            try:
                mail.logout()
            except Exception:
                pass

    except ValueError as e:
        return f"⚠️ {e}"
    except imaplib.IMAP4.error as e:
        return f"⚠️ IMAP ошибка: {e}"
    except Exception as e:
        log.warning("search_mail failed", exc_info=True)
        return f"⚠️ Ошибка: {repr(e)}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("read_inbox", {
            "name": "read_inbox",
            "description": "Прочитать последние письма из Яндекс.Почты. Возвращает список с отправителем, темой, датой и кратким превью.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Количество последних писем (по умолчанию 10)",
                        "default": 10,
                    },
                    "folder": {
                        "type": "string",
                        "description": "Папка для чтения (по умолчанию INBOX)",
                        "default": "INBOX",
                    },
                },
                "required": [],
            },
        }, _read_inbox),
        ToolEntry("search_mail", {
            "name": "search_mail",
            "description": "Поиск писем в Яндекс.Почте по отправителю, теме или дате.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Свободный текстовый поиск (ищет по теме письма)",
                        "default": "",
                    },
                    "sender": {
                        "type": "string",
                        "description": "Фильтр по отправителю (email или имя)",
                        "default": "",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Фильтр по теме письма",
                        "default": "",
                    },
                    "since": {
                        "type": "string",
                        "description": "Искать письма с этой даты (формат YYYY-MM-DD, например 2026-01-01)",
                        "default": "",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Папка для поиска (по умолчанию INBOX)",
                        "default": "INBOX",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное количество результатов (по умолчанию 20)",
                        "default": 20,
                    },
                },
                "required": [],
            },
        }, _search_mail),
    ]
