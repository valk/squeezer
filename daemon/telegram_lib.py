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


def send_message(text: str, cfg: TelegramConfig = None, timeout: int = 10) -> None:
    """Plain message send. The HUD status (mode/budget, TODO counts, latest
    worklog snippet) no longer rides along on every message — see
    update_bot_status, which keeps it live in the bot's own display name and
    a pinned message instead. Still nudges that update on every send so it
    tracks state at least as fresh as whatever prompted this message,
    without waiting for telegram_poll_loop's own tick."""
    cfg = cfg or TelegramConfig()
    data = urllib.parse.urlencode({"chat_id": cfg.allowed_chat_id, "text": text}).encode()
    req = urllib.request.Request(cfg.api_url("sendMessage"), data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        json.load(resp)
    try:
        update_bot_status(cfg)
    except Exception:
        pass  # never let a HUD-push failure look like a failed send


def _bot_status_state_path() -> Path:
    return _config.state_dir() / "telegram_bot_status.json"


def _load_bot_status_state() -> dict:
    path = _bot_status_state_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass  # corrupt/truncated (e.g. write interrupted mid-flight) — fall back to default below
    return {"title": None, "description": None, "message_id": None}


def _save_bot_status_state(state: dict) -> None:
    _config.atomic_write_text(_bot_status_state_path(), json.dumps(state, indent=2) + "\n")


def _call_telegram(cfg: TelegramConfig, method: str, params: dict, timeout: int = 10) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(cfg.api_url(method), data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def update_bot_status(cfg: TelegramConfig = None) -> None:
    """Keeps hud_status live and visible without it riding along on every
    message body: the bot's own display name mirrors the usage bar
    (setMyName — a global bot property, fine here since this is a
    single-owner bot), and one pinned message in the allowed chat carries
    the full details ("squeezed: N%, user: N%, ..." — see
    hud_status.current_status_line). A real Telegram chat *description*
    (setChatDescription) only works on groups/channels, not the private
    1:1 chat this bot's setup uses — a pinned message is the private-chat
    equivalent, and Bot API allows a bot to pin/edit its own messages there
    without needing admin rights the way a group would.

    Skips the API call entirely when the text hasn't changed since the last
    successful push (cached in state/telegram_bot_status.json) — avoids
    hammering setMyName/editMessageText on every poll tick and message send
    when nothing actually moved. Can raise (a network error, a malformed
    response) — send_message swallows that itself, and telegram_poll_loop's
    own broad except-and-retry around its whole iteration covers this call
    too."""
    cfg = cfg or TelegramConfig()
    title = hud_status.bot_title()
    description = hud_status.current_status_line(color=False)
    state = _load_bot_status_state()

    if title != state.get("title"):
        _call_telegram(cfg, "setMyName", {"name": title[:64]})
        state["title"] = title

    if description != state.get("description"):
        message_id = state.get("message_id")
        if message_id:
            try:
                _call_telegram(cfg, "editMessageText", {
                    "chat_id": cfg.allowed_chat_id, "message_id": message_id, "text": description,
                })
            except Exception:
                message_id = None  # pinned message likely deleted — fall through and recreate it
        if not message_id:
            sent = _call_telegram(cfg, "sendMessage", {"chat_id": cfg.allowed_chat_id, "text": description})
            message_id = sent["result"]["message_id"]
            _call_telegram(cfg, "pinChatMessage", {
                "chat_id": cfg.allowed_chat_id, "message_id": message_id, "disable_notification": True,
            })
        state["message_id"] = message_id
        state["description"] = description

    _save_bot_status_state(state)


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
