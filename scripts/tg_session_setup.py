#!/usr/bin/env python3
"""
Telethon Session Setup — one-time setup script.

Run once in a Colab cell to:
1. Authenticate with Telegram MTProto User API
2. Generate a session string → copy to Colab Secrets as TG_SESSION_STRING
3. Save a manifest of available chats to Drive (for choosing which to monitor)

Prerequisites — add to Colab Secrets:
    TG_API_ID       — integer, from https://my.telegram.org → App Configuration
    TG_API_HASH     — string,  from https://my.telegram.org → App Configuration
    TG_PHONE        — your phone in international format, e.g. +79161234567

Security rules:
    - Use a SECONDARY Telegram account (NOT your main one)
    - Enable 2FA on that secondary account
    - Session string = full account access — store ONLY in Colab Secrets
    - Never log, print to file, or commit the session string
    - Revoke at https://my.telegram.org → Active Sessions if compromised

Usage in Colab:
    !python /content/ouroboros_repo/scripts/tg_session_setup.py
"""

import asyncio
import json
import os
import pathlib
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def _ensure_telethon() -> None:
    try:
        import telethon  # noqa: F401
    except ImportError:
        print("Installing telethon...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "telethon", "-q"], check=True)

_ensure_telethon()

from telethon import TelegramClient                    # noqa: E402
from telethon.sessions import StringSession            # noqa: E402
from telethon.tl.types import Chat, Channel, User      # noqa: E402


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def get_secret(name: str) -> str:
    """Read from Colab Secrets first, then os.environ."""
    try:
        from google.colab import userdata  # type: ignore
        val = userdata.get(name)
        if val and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    val = os.environ.get(name, "")
    if not val:
        raise ValueError(
            f"Missing secret: '{name}'. "
            "Add it in Colab: left panel → 🔑 Secrets → Add new secret."
        )
    return val.strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 65)
    print("  Telethon Session Setup for Ouroboros Group Monitor")
    print("=" * 65)
    print()

    # --- Read credentials ---
    try:
        api_id   = int(get_secret("TG_API_ID"))
        api_hash = get_secret("TG_API_HASH")
        phone    = get_secret("TG_PHONE")
    except ValueError as e:
        print(f"❌ {e}")
        print()
        print("Steps:")
        print("  1. Go to https://my.telegram.org → Log in → App Configuration")
        print("  2. Create an app (any name, any platform)")
        print("  3. Copy 'App api_id' and 'App api_hash'")
        print("  4. Add to Colab Secrets: TG_API_ID, TG_API_HASH, TG_PHONE")
        return

    # --- Check for existing session ---
    session_str = None
    try:
        session_str = get_secret("TG_SESSION_STRING")
        print(f"✅ Found existing TG_SESSION_STRING (len={len(session_str)}). Reusing.")
    except ValueError:
        print(f"📱 No session string yet — will create one for {phone}.")

    # --- Build client ---
    client = TelegramClient(
        StringSession(session_str or ""),
        api_id,
        api_hash,
        system_version="4.16.30-vxCUSTOM",   # avoid triggering bot detection
    )

    # --- Connect & authenticate ---
    print()
    print("Connecting to Telegram...")
    await client.start(phone=phone)

    if not session_str:
        new_session = client.session.save()
        print()
        print("=" * 65)
        print("✅  AUTHENTICATED — SESSION STRING BELOW")
        print("=" * 65)
        print()
        print(new_session)
        print()
        print("=" * 65)
        print("ACTION REQUIRED:")
        print("  1. Copy the string above (entire line, no spaces)")
        print("  2. In Colab: left panel → 🔑 Secrets")
        print("  3. Add new secret: name=TG_SESSION_STRING, value=<paste>")
        print("  4. Enable 'Notebook access' toggle")
        print("=" * 65)
        print()
        print("⚠️  SECURITY:")
        print("  • This string = full Telegram account access")
        print("  • Store ONLY in Colab Secrets")
        print("  • Revoke at my.telegram.org → Active Sessions if leaked")
        print()

    # --- Account info ---
    me = await client.get_me()
    print(f"👤 Logged in as: {me.first_name} {getattr(me, 'last_name', '') or ''}"
          f" (@{getattr(me, 'username', None) or 'no username'})")
    print(f"   ID: {me.id} | Phone: {me.phone}")
    print()

    # --- List dialogs ---
    print("📋 Fetching dialogs (up to 100)...")
    dialogs = await client.get_dialogs(limit=100)

    groups:   list = []
    channels: list = []
    privates: list = []

    for d in dialogs:
        e = d.entity
        entry: dict = {
            "id":      d.id,
            "title":   d.name,
            "unread":  d.unread_count,
            "type":    type(e).__name__,
        }
        if isinstance(e, User):
            privates.append(entry)
        elif isinstance(e, Channel):
            entry["username"]     = getattr(e, "username", None)
            entry["members"]      = getattr(e, "participants_count", None)
            entry["is_megagroup"] = bool(getattr(e, "megagroup", False))
            (groups if entry["is_megagroup"] else channels).append(entry)
        elif isinstance(e, Chat):
            entry["members"] = getattr(e, "participants_count", None)
            groups.append(entry)

    print(f"\n  Groups / Supergroups: {len(groups)}")
    for g in groups[:25]:
        print(f"    [{g['id']:>14}] {g['title'][:50]:<50}  "
              f"{g.get('members', '?'):>6} members  {g['unread']} unread")

    print(f"\n  Channels: {len(channels)}")
    for c in channels[:10]:
        handle = f"@{c['username']}" if c.get("username") else "private"
        print(f"    [{c['id']:>14}] {c['title'][:50]:<50}  {handle}")

    # --- Save manifest ---
    drive_memory = pathlib.Path("/content/drive/MyDrive/Ouroboros/memory")
    if drive_memory.exists():
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "account": {
                "id":       me.id,
                "name":     f"{me.first_name} {getattr(me, 'last_name', '') or ''}".strip(),
                "username": getattr(me, "username", None),
                "phone":    me.phone,
            },
            "groups":   groups,
            "channels": channels,
        }
        out_path = drive_memory / "tg_chat_manifest.json"
        out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ Chat manifest saved: {out_path}")
        print("   Share the group IDs from this file when configuring monitoring.")
    else:
        print("\n⚠️  Drive not mounted — manifest not saved.")

    await client.disconnect()
    print("\n✅ Done.")


if __name__ == "__main__":
    asyncio.run(main())
