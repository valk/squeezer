#!/usr/bin/env python3
"""Shared Telegram Bot API helpers used by daemon/daemon.py and
mcp/telegram_server.py. Long-polling only (no inbound webhook/open port).
Never eval's or shell-interpolates message text."""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as _config  # noqa: E402
import hud_status  # noqa: E402


class TelegramConfig:
    def __init__(self):
        env = _config.load_env()
        self.token = env.get("TELEGRAM_BOT_TOKEN", "")
        self.allowed_chat_id = env.get("TELEGRAM_ALLOWED_CHAT_ID", "")
        self.owner_user_id = env.get("TELEGRAM_OWNER_USER_ID", "")
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set in SQUEEZER_HOME/.env")
        if not self.allowed_chat_id:
            raise RuntimeError("TELEGRAM_ALLOWED_CHAT_ID not set in SQUEEZER_HOME/.env")
        if not self.owner_user_id:
            raise RuntimeError("TELEGRAM_OWNER_USER_ID not set in SQUEEZER_HOME/.env")

    def api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"


def send_message(text: str, cfg: TelegramConfig = None, timeout: int = 10, include_hud: bool = True) -> None:
    """`include_hud` prepends the one-line HUD status (mode/budget, TODO
    counts, latest worklog snippet — see hud_status.py) as a header, so every
    message doubles as a status glance. Never lets a HUD-building bug break
    message delivery: falls back to the plain message on any error.
    color=False since Telegram renders plain text and would show raw ANSI
    escape codes as garbage rather than a colored bar."""
    cfg = cfg or TelegramConfig()
    if include_hud:
        try:
            text = f"{hud_status.current_status_line(color=False)}\n\n{text}"
        except Exception:
            pass
    data = urllib.parse.urlencode({"chat_id": cfg.allowed_chat_id, "text": text}).encode()
    req = urllib.request.Request(cfg.api_url("sendMessage"), data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        json.load(resp)


def get_updates(offset: int, cfg: TelegramConfig = None, timeout: int = 30):
    """Long-poll. Returns (updates, next_offset). Drops (and reports) any
    update that isn't both in the allowed chat AND actually sent by the
    owner's own Telegram account — verification lives here so both callers
    get it for free. Checking `from.id` (the message's real author) and not
    just `chat.id` (the conversation) matters the moment this bot is ever
    added to a group: chat_id alone would then accept messages from anyone
    in that group, not just the owner."""
    cfg = cfg or TelegramConfig()
    params = urllib.parse.urlencode({
        "offset": offset,
        "timeout": timeout,
        "allowed_updates": json.dumps(["message"]),
    })
    url = f"{cfg.api_url('getUpdates')}?{params}"
    with urllib.request.urlopen(url, timeout=timeout + 10) as resp:
        data = json.load(resp)

    if not data.get("ok"):
        return [], offset

    verified = []
    next_offset = offset
    for update in data.get("result", []):
        next_offset = max(next_offset, update["update_id"] + 1)
        msg = update.get("message")
        if not msg or "text" not in msg:
            continue
        chat_id = str(msg["chat"]["id"])
        sender_id = str(msg.get("from", {}).get("id", ""))
        if chat_id != str(cfg.allowed_chat_id) or not sender_id or sender_id != str(cfg.owner_user_id):
            print(
                f"WARNING: dropped message from unverified sender "
                f"(chat_id={chat_id}, from_id={sender_id or 'missing'})",
                flush=True,
            )
            continue
        verified.append(msg["text"])
    return verified, next_offset
