"""Email digest tool — classified summary of incoming mail.

Builds a 🔴/🟡/🟢 digest of recent emails using rules from
/content/drive/MyDrive/Ouroboros/memory/email_topics.md.

Classification:
  🔴 red    — requires attention (important sender to:me, flagged, urgency)
  🟡 yellow — project topic mention (ST Luce, БФК, etc.) regardless of To/Cc
  🟢 green  — informational, everything else

Tools:
  email_digest — generate classified email digest for a time window
"""
from __future__ import annotations

import imaplib
import email
import os
import pathlib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOPICS_PATH = "/content/drive/MyDrive/Ouroboros/memory/email_topics.md"
MSK_OFFSET = timedelta(hours=3)  # UTC+3


# ---------------------------------------------------------------------------
# Topics / rules loader
# ---------------------------------------------------------------------------

def _load_topics() -> dict:
    """Parse email_topics.md and return structured rules."""
    rules = {
        "topics": [],
        "important_senders": [],
        "ignore_domains": [],
        "ignore_addresses": [],
        "urgency_keywords": ["срочно", "urgent", "asap", "дедлайн", "не позднее"],
    }
    try:
        text = pathlib.Path(TOPICS_PATH).read_text(encoding="utf-8")
    except Exception:
        try:
            with open(TOPICS_PATH, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            return rules

    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## Темы"):
            section = "topics"
        elif line.startswith("## Важные отправители"):
            section = "senders"
        elif line.startswith("## Игнорируемые"):
            section = "ignore"
        elif line.startswith("## "):
            section = None
        elif line.startswith("- ") and section:
            val = line[2:].strip()
            if section == "topics" and val:
                rules["topics"].append(val.lower())
            elif section == "senders" and "@" in val:
                rules["important_senders"].append(val.lower())
            elif section == "ignore":
                if val.startswith("@"):
                    rules["ignore_domains"].append(val.lower())
                elif "@" in val:
                    rules["ignore_addresses"].append(val.lower())
    return rules


def _should_ignore(from_addr: str, rules: dict) -> bool:
    addr = from_addr.lower()
    for ign in rules["ignore_addresses"]:
        if ign in addr:
            return True
    for dom in rules["ignore_domains"]:
        if dom in addr:
            return True
    return False


def _classify(msg: dict, rules: dict, owner_email: str) -> str:
    """Return 'red', 'yellow', or 'green' classification.

    red    — requires attention (important sender to:me, flagged, urgency)
    yellow — project topic mention (ST Luce, БФК, etc.) regardless of To/Cc
    green  — everything else (informational)
    """
    from_addr = msg.get("from", "").lower()
    to_addr = msg.get("to", "").lower()
    subject = msg.get("subject", "").lower()
    preview = msg.get("preview", "").lower()
    is_flagged = msg.get("flagged", False)
    text_bucket = subject + " " + preview
    owner = owner_email.lower()

    # Red: flagged Important
    if is_flagged:
        return "red"
    # Red: important sender where owner is in To (not Cc)
    for sender in rules["important_senders"]:
        if sender in from_addr and owner in to_addr:
            return "red"
    # Red: urgency keywords
    for kw in rules["urgency_keywords"]:
        if kw in text_bucket:
            return "red"

    # Yellow: project topic mentioned — always in digest regardless of To/Cc
    for topic in rules["topics"]:
        if topic in text_bucket:
            return "yellow"

    return "green"


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def _get_credentials() -> tuple[str, str]:
    email_addr = os.environ.get("YANDEX_EMAIL", "").strip()
    password = os.environ.get("YANDEX_APP_PASSWORD", "").strip()
    if not email_addr or not password:
        env_file = os.environ.get("OUROBOROS_SESSION_ENV_FILE", "/tmp/ouroboros.env")
        try:
            with open(env_file) as f:
                for line in f:
                    if "=" not in line:
                        continue
                    k, _, v = line.strip().partition("=")
                    if k == "YANDEX_EMAIL" and not email_addr:
                        email_addr = v.strip()
                    if k == "YANDEX_APP_PASSWORD" and not password:
                        password = v.strip()
        except Exception:
            pass
    return email_addr, password


def _decode_str(value) -> str:
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
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
            if body.strip():
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
    return body[:800].strip()


def _connect() -> imaplib.IMAP4_SSL:
    email_addr, password = _get_credentials()
    if not email_addr or not password:
        raise RuntimeError("Yandex credentials not available")
    conn = imaplib.IMAP4_SSL("imap.yandex.ru", 993)
    conn.login(email_addr, password)
    return conn


def _fetch_since(hours: int) -> list[dict]:
    """Fetch messages from the last N hours. Returns list of dicts."""
    conn = _connect()
    conn.select("INBOX")

    since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_str = since_dt.strftime("%d-%b-%Y")  # e.g. 25-Feb-2026

    status, data = conn.uid("search", None, f"SINCE {since_str}")
    if status != "OK" or not data[0]:
        conn.logout()
        return []

    uids = list(reversed(data[0].split()))  # newest first

    messages = []
    uid_str = b",".join(uids)
    status2, raw_data = conn.uid("fetch", uid_str, "(RFC822 FLAGS)")
    if status2 != "OK":
        conn.logout()
        return []

    for item in raw_data:
        if not isinstance(item, tuple):
            continue
        header_part = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
        flags_match = re.search(r"FLAGS \(([^)]*)\)", header_part)
        flags_str = flags_match.group(1) if flags_match else ""
        is_flagged = "\\Flagged" in flags_str or "\\Important" in flags_str

        msg = email.message_from_bytes(item[1])
        from_ = _decode_str(msg.get("From", ""))
        to_ = _decode_str(msg.get("To", ""))
        cc_ = _decode_str(msg.get("Cc", ""))
        subject = _decode_str(msg.get("Subject", ""))
        date_raw = msg.get("Date", "")
        try:
            date_dt = parsedate_to_datetime(date_raw)
            if date_dt < since_dt:
                continue
            date_str = (date_dt + MSK_OFFSET).strftime("%d.%m %H:%M")
        except Exception:
            date_str = date_raw

        body = _get_body(msg)
        messages.append({
            "from": from_,
            "to": to_,
            "cc": cc_,
            "subject": subject,
            "date": date_str,
            "preview": body,
            "flagged": is_flagged,
        })

    conn.logout()
    return messages


# ---------------------------------------------------------------------------
# Digest builder
# ---------------------------------------------------------------------------

def _build_digest(messages: list[dict], rules: dict, owner_email: str, hours: int) -> str:
    red, yellow, green = [], [], []
    skipped = 0

    for msg in messages:
        if _should_ignore(msg["from"], rules):
            skipped += 1
            continue
        cls = _classify(msg, rules, owner_email)
        if cls == "red":
            red.append(msg)
        elif cls == "yellow":
            yellow.append(msg)
        else:
            green.append(msg)

    now_msk = datetime.now(timezone.utc) + MSK_OFFSET
    lines = [
        f"📧 Дайджест почты — {now_msk.strftime('%d.%m.%Y %H:%M')} МСК",
        f"Период: последние {hours} ч.  |  Всего: {len(messages)}  |  Пропущено рассылок: {skipped}",
        "",
    ]

    if red:
        lines.append(f"🔴 ТРЕБУЮТ ОТВЕТА ({len(red)}):")
        lines.append("")
        for m in red:
            lines.append(f"  [{m['date']}] {m['from']}")
            lines.append(f"  Тема: {m['subject']}")
            if m["preview"]:
                lines.append(f"  {m['preview'][:200]}")
            lines.append("")
    else:
        lines.append("🔴 Срочных писем нет.")
        lines.append("")

    if yellow:
        lines.append(f"🟡 ПО ПРОЕКТАМ ({len(yellow)}):")
        lines.append("")
        for m in yellow:
            lines.append(f"  [{m['date']}] {m['from']}")
            lines.append(f"  Тема: {m['subject']}")
            if m["preview"]:
                lines.append(f"  {m['preview'][:200]}")
            lines.append("")
    else:
        lines.append("🟡 По проектам — нет.")
        lines.append("")

    if green:
        lines.append(f"🟢 К СВЕДЕНИЮ ({len(green)}):")
        lines.append("")
        for m in green:
            lines.append(f"  [{m['date']}] {m['from']}")
            lines.append(f"  Тема: {m['subject']}")
            lines.append("")
    else:
        lines.append("🟢 Нет писем к сведению.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

def _email_digest(ctx: ToolContext, hours: int = 5) -> str:
    """Generate a classified email digest for the last N hours.

    Reads rules from email_topics.md (topics, important senders, ignore list).
    Classifies each email as:
      🔴 red    — requires attention
      🟡 yellow — project topic (ST Luce, БФК, etc.), regardless of To/Cc
      🟢 green  — informational

    Args:
        hours: How many hours back to scan (default 5, use 19 for morning digest).
    """
    hours = max(1, min(int(hours), 48))
    owner_email, _ = _get_credentials()
    rules = _load_topics()
    try:
        messages = _fetch_since(hours)
    except Exception as e:
        return f"Ошибка подключения к почте: {e}"

    if not messages:
        return f"За последние {hours} ч. новых писем не найдено."

    return _build_digest(messages, rules, owner_email, hours)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="email_digest",
            description=(
                "Generate a classified email digest for the last N hours. "
                "Classifies emails as 🔴 (requires attention), "
                "🟡 (project topic — ST Luce, БФК, etc.), or 🟢 (informational) "
                "based on email_topics.md rules."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "How many hours back to scan (default 5, use 19 for morning digest)",
                        "default": 5,
                    }
                },
                "required": [],
            },
            handler=_email_digest,
        )
    ]
