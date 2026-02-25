"""Email digest tool — twice-daily classified email summary.

Reads classification rules from Drive: memory/email_topics.md
Classifies into three buckets:
  🔴 — requires owner's reply (waiting for response)
  🟡 — thematic / FYI (matches tracked topics, or important but no urgent action)
  🟢 — informational (worth knowing)

Ignores addresses/domains listed in email_topics.md.
Triggered by scheduler at 09:00 and 14:00 MSK, or called manually via /digest.
"""

from __future__ import annotations

import imaplib
import email
import os
import re
import json
import datetime
import pathlib
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

# ---------------------------------------------------------------------------
# Credential helpers (minimal — avoid importing yandex_mail to prevent circular)
# ---------------------------------------------------------------------------

def _get_credentials() -> tuple[str, str]:
    email_addr = os.environ.get("YANDEX_EMAIL", "").strip()
    password = os.environ.get("YANDEX_APP_PASSWORD", "").strip()
    if not email_addr or not password:
        env_file = os.environ.get("OUROBOROS_SESSION_ENV_FILE", "/tmp/ouroboros.env")
        try:
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    if k.strip() == "YANDEX_EMAIL" and not email_addr:
                        email_addr = v.strip()
                    if k.strip() == "YANDEX_APP_PASSWORD" and not password:
                        password = v.strip()
        except Exception:
            pass
    return email_addr, password


def _connect() -> imaplib.IMAP4_SSL:
    email_addr, password = _get_credentials()
    if not email_addr or not password:
        raise RuntimeError("YANDEX_EMAIL or YANDEX_APP_PASSWORD not available.")
    conn = imaplib.IMAP4_SSL("imap.yandex.ru", 993)
    conn.login(email_addr, password)
    return conn


def _decode_header_str(value) -> str:
    if value is None:
        return ""
    parts = decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
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


# ---------------------------------------------------------------------------
# Config parser
# ---------------------------------------------------------------------------

def _parse_topics_file(drive_root: pathlib.Path) -> dict:
    """Parse memory/email_topics.md and return structured config."""
    result = {
        "topics": [],
        "important_senders": [],
        "ignore_domains": [],
        "ignore_addresses": [],
    }
    try:
        path = drive_root / "memory" / "email_topics.md"
        text = path.read_text(encoding="utf-8")
    except Exception:
        return result

    section = None
    for line in text.splitlines():
        line_stripped = line.strip()
        low = line_stripped.lower()

        # Detect section headers
        if "темы для отслеживания" in low:
            section = "topics"
        elif "важные отправители" in low:
            section = "senders"
        elif "игнорируемые" in low:
            section = "ignore"
        elif line_stripped.startswith("##"):
            section = None  # unknown section, reset

        if not line_stripped.startswith("- "):
            continue

        item = line_stripped[2:].strip()
        if not item:
            continue

        if section == "topics":
            result["topics"].append(item)
        elif section == "senders":
            result["important_senders"].append(item.lower())
        elif section == "ignore":
            if item.startswith("@"):
                result["ignore_domains"].append(item.lower())
            else:
                result["ignore_addresses"].append(item.lower())

    return result


def _should_ignore(from_addr: str, config: dict) -> bool:
    """Return True if this sender should be ignored."""
    addr_low = from_addr.lower()
    for domain in config["ignore_domains"]:
        if domain in addr_low:
            return True
    for addr in config["ignore_addresses"]:
        if addr in addr_low:
            return True
    return False


# ---------------------------------------------------------------------------
# IMAP fetch
# ---------------------------------------------------------------------------

def _fetch_recent_messages(conn: imaplib.IMAP4_SSL, since_hours: int) -> list[dict]:
    """Fetch messages from INBOX received in the last `since_hours` hours."""
    conn.select("INBOX")

    since_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=since_hours)
    since_str = since_dt.strftime("%d-%b-%Y")

    status, data = conn.uid("search", None, f"SINCE {since_str}")
    if status != "OK" or not data[0]:
        return []

    all_uids = data[0].split()
    if not all_uids:
        return []

    uid_str = b",".join(all_uids)
    status2, raw_data = conn.uid("fetch", uid_str, "(RFC822)")
    if status2 != "OK":
        return []

    messages = []
    for response in raw_data:
        if not isinstance(response, tuple):
            continue
        msg = email.message_from_bytes(response[1])
        subject = _decode_header_str(msg.get("Subject", ""))
        from_ = _decode_header_str(msg.get("From", ""))
        to_ = _decode_header_str(msg.get("To", ""))
        cc_ = _decode_header_str(msg.get("Cc", ""))
        date_raw = msg.get("Date", "")
        try:
            date_dt = parsedate_to_datetime(date_raw)
            date_str = date_dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_str = date_raw
            date_dt = None
        body = _get_body(msg)

        messages.append({
            "from": from_,
            "to": to_,
            "cc": cc_,
            "subject": subject,
            "date": date_str,
            "date_dt": date_dt,
            "preview": body,
        })

    # Sort newest first
    def _sort_key(m):
        d = m.get("date_dt")
        if d is None:
            return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d

    messages.sort(key=_sort_key, reverse=True)
    return messages


# ---------------------------------------------------------------------------
# LLM classification
# ---------------------------------------------------------------------------

def _classify_with_llm(messages: list[dict], config: dict, owner_email: str) -> dict:
    """Use light LLM to classify messages into red/yellow/green buckets."""
    if not messages:
        return {"red": [], "yellow": [], "green": []}

    try:
        from ouroboros.llm import LLMClient, DEFAULT_LIGHT_MODEL

        msg_lines = []
        for i, m in enumerate(messages):
            msg_lines.append(
                f"{i+1}. От: {m['from']} | Кому: {m['to']} | Копия: {m['cc']}\n"
                f"   Тема: {m['subject']}\n"
                f"   Дата: {m['date']}\n"
                f"   Текст: {m['preview'][:300]}"
            )
        msgs_text = "\n\n".join(msg_lines)

        topics_str = ", ".join(config["topics"]) if config["topics"] else "нет"
        senders_str = ", ".join(config["important_senders"]) if config["important_senders"] else "нет"

        prompt = f"""Ты аналитик почты. Классифицируй письма на три группы.

Адрес владельца: {owner_email}

Важные отправители (важны ТОЛЬКО если владелец стоит в поле "Кому", не в Копии):
{senders_str}

Темы для отслеживания: {topics_str}

Правила:
🔴 (red) — письмо адресовано владельцу (он в "Кому", не в Копии), от важного отправителя ИЛИ содержит прямой вопрос/запрос/признаки срочности ("срочно", "до дата", "ASAP", "дедлайн")
🟡 (yellow) — упоминается одна из отслеживаемых тем, ИЛИ важное но без срочного ответа
🟢 (green) — всё остальное (информационные, деловые без срочности)

Ответь ТОЛЬКО JSON без markdown, ключи "red", "yellow", "green" — массивы номеров писем:
{{"red": [1, 3], "yellow": [2], "green": [4, 5]}}

Письма:
{msgs_text}"""

        client = LLMClient()
        model = os.environ.get("OUROBOROS_MODEL_LIGHT") or DEFAULT_LIGHT_MODEL
        response = client.complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.1,
        )
        text = response.strip()
        text = re.sub(r"```[a-z]*\n?", "", text).strip().rstrip("```")
        result = json.loads(text)
        return {
            "red": [int(x) for x in result.get("red", [])],
            "yellow": [int(x) for x in result.get("yellow", [])],
            "green": [int(x) for x in result.get("green", [])],
        }
    except Exception as e:
        # Fallback: everything to green
        all_nums = list(range(1, len(messages) + 1))
        return {"red": [], "yellow": [], "green": all_nums, "_error": str(e)}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_digest(messages: list[dict], classification: dict, period_label: str) -> str:
    """Format classified messages into a Telegram-friendly text."""
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%d.%m %H:%M UTC")
    lines = [f"📬 Дайджест почты — {period_label} ({now_str})", ""]

    def fmt_msg(idx: int) -> str:
        m = messages[idx - 1]
        # Extract clean sender name
        from_str = m["from"]
        name_match = re.match(r'^"?([^"<]+)"?\s*<', from_str)
        from_short = name_match.group(1).strip() if name_match else from_str.split("@")[0]
        date_short = m["date"][5:]  # strip year, keep MM-DD HH:MM
        preview = m["preview"][:180].replace("\n", " ").strip()
        return (
            f"  • [{date_short}] {from_short}\n"
            f"    📎 {m['subject']}\n"
            f"    {preview}"
        )

    red_nums = classification.get("red", [])
    yellow_nums = classification.get("yellow", [])
    green_nums = classification.get("green", [])

    valid_red = [n for n in red_nums if 1 <= n <= len(messages)]
    valid_yellow = [n for n in yellow_nums if 1 <= n <= len(messages)]
    valid_green = [n for n in green_nums if 1 <= n <= len(messages)]

    if valid_red:
        lines.append(f"🔴 Требуют ответа ({len(valid_red)}):")
        for n in valid_red:
            lines.append(fmt_msg(n))
        lines.append("")

    if valid_yellow:
        lines.append(f"🟡 Тематические / к сведению ({len(valid_yellow)}):")
        for n in valid_yellow:
            lines.append(fmt_msg(n))
        lines.append("")

    if valid_green:
        lines.append(f"🟢 Информация ({len(valid_green)}):")
        for n in valid_green:
            lines.append(fmt_msg(n))
        lines.append("")

    total = len(messages)
    lines.append(
        f"Итого: {total} писем | 🔴 {len(valid_red)} | 🟡 {len(valid_yellow)} | 🟢 {len(valid_green)}"
    )

    if "_error" in classification:
        lines.append(f"⚠️ LLM fallback: {classification['_error'][:100]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

def _email_digest(ctx: ToolContext, hours: int = 5, label: str = "") -> str:
    """Generate a classified email digest for the last N hours.

    Args:
        hours: How many hours back to scan. Default 5 (afternoon). Morning = 19.
        label: Human-readable period label shown in the digest header.
    """
    hours = max(1, min(int(hours), 48))
    period_label = label or f"последние {hours} ч."

    try:
        config = _parse_topics_file(ctx.drive_root)
        owner_email, _ = _get_credentials()

        conn = _connect()
        raw_messages = _fetch_recent_messages(conn, since_hours=hours)
        conn.logout()

        # Filter ignored senders
        filtered = [m for m in raw_messages if not _should_ignore(m["from"], config)]
        ignored_count = len(raw_messages) - len(filtered)

        if not filtered:
            note = f" (отфильтровано рассылок: {ignored_count})" if ignored_count else ""
            return f"📬 Дайджест за {period_label}: новых писем нет{note}"

        classification = _classify_with_llm(filtered, config, owner_email)
        digest = _format_digest(filtered, classification, period_label)

        if ignored_count:
            digest += f"\n_(отфильтровано рассылок: {ignored_count})_"

        return digest

    except Exception as e:
        return f"❌ Ошибка дайджеста: {e}"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="email_digest",
            schema={
                "name": "email_digest",
                "description": (
                    "Generate a classified email digest for the last N hours. "
                    "Classifies into 🔴 (requires reply), 🟡 (thematic/FYI), 🟢 (info). "
                    "Filters ignored senders. Rules from Drive memory/email_topics.md."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "integer",
                            "description": "Hours back to scan (default 5; morning digest = 19)",
                        },
                        "label": {
                            "type": "string",
                            "description": "Human-readable period label (e.g. 'утро 9:00')",
                        },
                    },
                    "required": [],
                },
            },
            handler=_email_digest,
            timeout_sec=90,
        )
    ]
