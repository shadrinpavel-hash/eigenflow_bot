"""Email digest tool — classified summary of incoming mail.

Builds a 🔴/🟡/🟢 digest of recent emails using rules from
/content/drive/MyDrive/Ouroboros/memory/email_topics.md.

Classification:
  🔴 red    — requires attention (important sender to:me, flagged, urgency)
  🟡 yellow — project topic mention (ST Luce, БФК, etc.) regardless of To/Cc
              also: outgoing messages without a reply (tracked assignments)
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

# Folders to skip when scanning all incoming folders
SKIP_FOLDERS = {
    "trash", "spam", "junk", "drafts", "sent", "sent messages",
    "шаблоны", "спам", "удалённые", "удаленные", "черновики", "прочее",
    "templates",
}

# Candidate names for Sent folder
SENT_FOLDER_CANDIDATES = [
    "Sent", "Sent Messages", "Отправленные", "INBOX.Sent", "Sent Items",
]


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
    """Return 'red', 'yellow', or 'green' classification."""
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


def _parse_folder_name(raw_line: bytes) -> str | None:
    """Extract folder name from IMAP LIST response line."""
    try:
        decoded = raw_line.decode("utf-8", errors="replace")
    except Exception:
        decoded = str(raw_line)
    # Format: (\Flags) "delimiter" "folder name"  OR  (\Flags) "delimiter" folder
    # Strip flags and delimiter, return folder name
    match = re.search(r'"[^"]*"\s+"?([^"]+)"?\s*$', decoded)
    if not match:
        match = re.search(r'"[^"]*"\s+(.+)$', decoded)
    if match:
        name = match.group(1).strip().strip('"')
        return name
    return None


def _list_inbox_folders(conn: imaplib.IMAP4_SSL) -> list[str]:
    """Return list of folders to scan (excluding skip list)."""
    status, folder_list = conn.list()
    if status != "OK":
        return ["INBOX"]

    folders = []
    for item in folder_list:
        if not isinstance(item, bytes):
            continue
        name = _parse_folder_name(item)
        if not name:
            continue
        # Skip noselect folders
        if b"\\Noselect" in item:
            continue
        # Skip excluded folders (case-insensitive)
        name_lower = name.lower()
        skip = False
        for skip_name in SKIP_FOLDERS:
            if skip_name in name_lower:
                skip = True
                break
        if not skip:
            folders.append(name)

    # Always include INBOX if not present
    if not any(f.upper() == "INBOX" for f in folders):
        folders.insert(0, "INBOX")

    return folders if folders else ["INBOX"]


def _fetch_from_folder(conn: imaplib.IMAP4_SSL, folder: str, since_dt: datetime) -> list[dict]:
    """Fetch messages from a single folder since given datetime."""
    try:
        status, _ = conn.select(f'"{folder}"', readonly=True)
        if status != "OK":
            # Try without quotes
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                return []
    except Exception:
        return []

    since_str = since_dt.strftime("%d-%b-%Y")
    try:
        status, data = conn.uid("search", None, f"SINCE {since_str}")
    except Exception:
        return []

    if status != "OK" or not data[0]:
        return []

    uids = list(reversed(data[0].split()))
    if not uids:
        return []

    uid_str = b",".join(uids)
    try:
        status2, raw_data = conn.uid("fetch", uid_str, "(RFC822 FLAGS)")
    except Exception:
        return []

    if status2 != "OK" or not raw_data:
        return []

    messages = []
    for item in raw_data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        header_part = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
        flags_match = re.search(r"FLAGS \(([^)]*)\)", header_part)
        flags_str = flags_match.group(1) if flags_match else ""
        is_flagged = "\\Flagged" in flags_str or "\\Important" in flags_str

        try:
            msg = email.message_from_bytes(item[1])
        except Exception:
            continue

        from_ = _decode_str(msg.get("From", ""))
        to_ = _decode_str(msg.get("To", ""))
        cc_ = _decode_str(msg.get("Cc", ""))
        subject = _decode_str(msg.get("Subject", ""))
        message_id = _decode_str(msg.get("Message-ID", "")).strip()
        in_reply_to = _decode_str(msg.get("In-Reply-To", "")).strip()
        references = _decode_str(msg.get("References", "")).strip()
        date_raw = msg.get("Date", "")

        try:
            date_dt = parsedate_to_datetime(date_raw)
            if date_dt.tzinfo is None:
                date_dt = date_dt.replace(tzinfo=timezone.utc)
            if date_dt < since_dt:
                continue
            date_str = (date_dt.astimezone(timezone.utc) + MSK_OFFSET).strftime("%d.%m %H:%M")
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
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references": references,
            "folder": folder,
        })

    return messages


def _fetch_since(hours: int) -> list[dict]:
    """Fetch messages from ALL inbox folders (except excluded) for the last N hours.
    Deduplicates by Message-ID."""
    conn = _connect()
    since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)

    folders = _list_inbox_folders(conn)
    all_messages: list[dict] = []
    seen_ids: set[str] = set()

    for folder in folders:
        msgs = _fetch_from_folder(conn, folder, since_dt)
        for m in msgs:
            mid = m.get("message_id", "")
            if mid and mid in seen_ids:
                continue  # dedup
            if mid:
                seen_ids.add(mid)
            all_messages.append(m)

    conn.logout()
    return all_messages


def _fetch_sent_since(hours: int) -> list[dict]:
    """Fetch sent messages from the last N hours to track unanswered assignments."""
    conn = _connect()
    since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)

    sent_folder = None
    for candidate in SENT_FOLDER_CANDIDATES:
        try:
            status, _ = conn.select(f'"{candidate}"', readonly=True)
            if status == "OK":
                sent_folder = candidate
                break
        except Exception:
            continue
        try:
            status, _ = conn.select(candidate, readonly=True)
            if status == "OK":
                sent_folder = candidate
                break
        except Exception:
            continue

    if not sent_folder:
        # Try to find via LIST
        status, folder_list = conn.list()
        if status == "OK":
            for item in folder_list:
                if not isinstance(item, bytes):
                    continue
                name = _parse_folder_name(item)
                if name and any(s in name.lower() for s in ["sent", "отправленные"]):
                    try:
                        st, _ = conn.select(f'"{name}"', readonly=True)
                        if st == "OK":
                            sent_folder = name
                            break
                    except Exception:
                        pass

    if not sent_folder:
        conn.logout()
        return []

    msgs = _fetch_from_folder(conn, sent_folder, since_dt)
    conn.logout()
    return msgs


# ---------------------------------------------------------------------------
# Digest builder
# ---------------------------------------------------------------------------

def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd: prefixes for matching."""
    s = subject.strip().lower()
    for prefix in ["re:", "fwd:", "fw:", "ответ:", "пересылка:"]:
        while s.startswith(prefix):
            s = s[len(prefix):].strip()
    return s



# ---------------------------------------------------------------------------
# LLM client (for smart reclassification)
# ---------------------------------------------------------------------------
_llm_client = None

def _get_llm():
    global _llm_client
    if _llm_client is None:
        try:
            from ouroboros.llm import LLMClient
            _llm_client = LLMClient()
        except Exception:
            pass
    return _llm_client


def _llm_reclassify_reds(red_candidates: list[dict]) -> list[dict]:
    """Use LLM to filter out messages that do NOT actually require a response.
    
    Returns only those messages that genuinely require owner's action.
    If LLM is unavailable, returns all candidates unchanged.
    """
    if not red_candidates:
        return []
    
    client = _get_llm()
    if client is None:
        return red_candidates
    
    # Build batch classification prompt
    items = []
    for i, m in enumerate(red_candidates):
        items.append(
            f"{i+1}. От: {m['from']} | Тема: {m['subject']} | "
            f"Превью: {m['preview'][:300]}"
        )
    
    prompt = (
        "Ты помощник по управлению почтой для делового человека.\n"
        "Ниже список писем, которые предварительно помечены как требующие ответа.\n"
        "Определи, какие из них ДЕЙСТВИТЕЛЬНО требуют ответа или действия от владельца ящика,\n"
        "а какие — это просто ответы на его запросы, отчёты, подтверждения, информирование,\n"
        "которые не требуют ответного действия.\n\n"
        "Примеры НЕ требующих ответа:\n"
        "- Согласен / OK / Принято в ответ на поручение\n"
        "- Подтверждение получения / выполнения задачи\n"
        "- Информационный отчёт о статусе\n"
        "- Время есть / Успеем\n\n"
        "Отвечай ТОЛЬКО JSON-массивом номеров писем, которые требуют ответа/действия.\n"
        "Пример: [1, 3, 5]\n\n"
        "Письма:\n"
        + "\n".join(items)
    )
    
    try:
        import json, re
        msg, _usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model="google/gemini-2.0-flash-001",
            max_tokens=300,
            reasoning_effort="low",
        )
        text = ""
        if isinstance(msg, dict):
            content_val = msg.get("content", "")
            if isinstance(content_val, str):
                text = content_val
            elif isinstance(content_val, list):
                for part in content_val:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text += part.get("text", "")
        m = re.search(r"\[([\d,\s]*)\]", text)
        if m:
            indices = json.loads(m.group(0))
            result = [red_candidates[i-1] for i in indices if 1 <= i <= len(red_candidates)]
            return result
    except Exception:
        pass
    
    return red_candidates

def _build_digest(
    messages: list[dict],
    rules: dict,
    owner_email: str,
    hours: int,
    sent_messages: list[dict] | None = None,
) -> str:
    red, yellow, green = [], [], []
    skipped = 0
    sent_messages = sent_messages or []

    # Build set of subjects for reply detection
    incoming_subjects = {_normalize_subject(m.get("subject", "")) for m in messages}
    incoming_message_ids = {m.get("message_id", "") for m in messages if m.get("message_id")}
    # Collect all in-reply-to references from incoming
    incoming_reply_refs: set[str] = set()
    for m in messages:
        for ref in (m.get("in_reply_to", ""), m.get("references", "")):
            for r in ref.split():
                incoming_reply_refs.add(r.strip())

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

    # LLM reclassification: filter out red messages that don't require action
    red = _llm_reclassify_reds(red)

    # Check sent messages for unanswered assignments
    unanswered = []
    for sent in sent_messages:
        subject_norm = _normalize_subject(sent.get("subject", ""))
        sent_id = sent.get("message_id", "")

        # Skip if a reply exists: matching subject in incoming, OR sent message_id in reply refs
        has_reply = (
            subject_norm in incoming_subjects
            or (sent_id and sent_id in incoming_reply_refs)
        )
        if not has_reply:
            # Only track if subject relates to known topics (to avoid noise)
            topic_match = any(
                t in sent.get("subject", "").lower() or t in sent.get("preview", "").lower()
                for t in rules["topics"]
            )
            if topic_match:
                unanswered.append(sent)

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

    if yellow or unanswered:
        total_y = len(yellow) + len(unanswered)
        lines.append(f"🟡 ПО ПРОЕКТАМ ({total_y}):")
        lines.append("")
        for m in yellow:
            lines.append(f"  [{m['date']}] {m['from']}")
            lines.append(f"  Тема: {m['subject']}")
            if m["preview"]:
                lines.append(f"  {m['preview'][:200]}")
            lines.append("")
        for m in unanswered:
            lines.append(f"  [→ Поручение без ответа] [{m['date']}] Кому: {m['to']}")
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

    Scans ALL inbox folders (except Trash, Spam, Drafts, Прочее, etc.)
    plus the Sent folder to track unanswered assignments.

    Classification:
      🔴 red    — requires attention (important sender, flagged, urgency)
      🟡 yellow — project topic (ST Luce, БФК, etc.) + outgoing without reply
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
        return f"Ошибка подключения к почте (входящие): {e}"

    try:
        sent_messages = _fetch_sent_since(hours)
    except Exception as e:
        sent_messages = []
        # Non-fatal — continue without sent tracking

    if not messages and not sent_messages:
        return f"За последние {hours} ч. новых писем не найдено."

    return _build_digest(messages, rules, owner_email, hours, sent_messages)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="email_digest",
            description=(
                "Generate a classified email digest for the last N hours. "
                "Scans ALL inbox folders (not just INBOX) plus Sent folder. "
                "Classifies emails as 🔴 (requires attention), "
                "🟡 (project topic — ST Luce, БФК, etc. — or unanswered outgoing), "
                "or 🟢 (informational) based on email_topics.md rules."
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
