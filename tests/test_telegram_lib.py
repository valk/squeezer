"""Minimal tests for the owner-verification logic in daemon/telegram_lib.py:
inbound Telegram messages must match both the allowed chat AND the owner's
own sender id, not just the chat."""
import importlib.util
import json
import urllib.parse
from pathlib import Path

import pytest

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("telegram_lib", SQUEEZER_DIR / "daemon" / "telegram_lib.py")
telegram_lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telegram_lib)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _cfg():
    cfg = telegram_lib.TelegramConfig.__new__(telegram_lib.TelegramConfig)
    cfg.token = "test-token"
    cfg.allowed_chat_id = "111"
    cfg.owner_user_id = "111"
    return cfg


def _updates_response(monkeypatch, messages):
    """messages: list of (chat_id, from_id_or_None) tuples -> builds a fake
    getUpdates payload and patches urlopen to return it."""
    result = []
    for i, (chat_id, from_id) in enumerate(messages):
        msg = {"chat": {"id": chat_id}, "text": f"msg{i}"}
        if from_id is not None:
            msg["from"] = {"id": from_id}
        result.append({"update_id": i, "message": msg})
    payload = {"ok": True, "result": result}
    monkeypatch.setattr(
        telegram_lib.urllib.request, "urlopen", lambda *a, **k: FakeResponse(payload)
    )


def test_accepts_message_from_owner_in_allowed_chat(monkeypatch):
    _updates_response(monkeypatch, [(111, 111)])
    verified, _ = telegram_lib.get_updates(0, _cfg())
    assert verified == ["msg0"]


def test_drops_message_from_other_sender_in_same_chat(monkeypatch):
    # Simulates the bot being in a group: chat_id matches, but the sender
    # isn't the owner.
    _updates_response(monkeypatch, [(111, 999)])
    verified, _ = telegram_lib.get_updates(0, _cfg())
    assert verified == []


def test_drops_message_from_wrong_chat(monkeypatch):
    _updates_response(monkeypatch, [(222, 111)])
    verified, _ = telegram_lib.get_updates(0, _cfg())
    assert verified == []


def test_drops_message_missing_sender(monkeypatch):
    _updates_response(monkeypatch, [(111, None)])
    verified, _ = telegram_lib.get_updates(0, _cfg())
    assert verified == []


def test_offset_advances_even_for_dropped_messages(monkeypatch):
    _updates_response(monkeypatch, [(222, 111)])
    _, next_offset = telegram_lib.get_updates(0, _cfg())
    assert next_offset == 1


def test_config_requires_owner_user_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_OWNER_USER_ID", raising=False)
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_CHAT_ID=111\n"
    )
    with pytest.raises(RuntimeError, match="TELEGRAM_OWNER_USER_ID"):
        telegram_lib.TelegramConfig()


def _sent_request(monkeypatch):
    """Patches urlopen to succeed and captures the outgoing Request."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["request"] = req
        return FakeResponse({"ok": True})

    monkeypatch.setattr(telegram_lib.urllib.request, "urlopen", fake_urlopen)
    return captured


def _sent_text(captured):
    return urllib.parse.parse_qs(captured["request"].data.decode())["text"][0]


def test_send_message_sends_plain_text(monkeypatch):
    """The HUD status header no longer rides along on every message body —
    see update_bot_status, which keeps it live in the bot's name/pinned
    message instead."""
    captured = _sent_request(monkeypatch)
    monkeypatch.setattr(telegram_lib, "update_bot_status", lambda cfg: None)

    telegram_lib.send_message("hello", _cfg())

    assert _sent_text(captured) == "hello"


def test_send_message_nudges_bot_status_update(monkeypatch):
    captured = _sent_request(monkeypatch)
    calls = []
    monkeypatch.setattr(telegram_lib, "update_bot_status", lambda cfg: calls.append(cfg))

    cfg = _cfg()
    telegram_lib.send_message("hello", cfg)

    assert calls == [cfg]


def test_send_message_swallows_bot_status_update_failure(monkeypatch):
    """A broken HUD/status push must not look like a failed message send."""
    captured = _sent_request(monkeypatch)

    def boom(cfg):
        raise RuntimeError("broken state file")

    monkeypatch.setattr(telegram_lib, "update_bot_status", boom)

    telegram_lib.send_message("hello", _cfg())  # must not raise

    assert _sent_text(captured) == "hello"


def _bot_status_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.setattr(telegram_lib.hud_status, "current_status_line", lambda **kw: "squeezed: 18%, user: 16%")


def test_update_bot_status_creates_pinned_message(tmp_path, monkeypatch):
    _bot_status_env(tmp_path, monkeypatch)
    calls = []

    def fake_call(cfg, method, params, timeout=10):
        calls.append((method, params))
        if method == "sendMessage":
            return {"result": {"message_id": 42}}
        return {"ok": True}

    monkeypatch.setattr(telegram_lib, "_call_telegram", fake_call)

    telegram_lib.update_bot_status(_cfg())

    methods = [c[0] for c in calls]
    assert methods == ["sendMessage", "pinChatMessage"]
    assert calls[1][1]["message_id"] == 42

    state = json.loads((tmp_path / "state" / "telegram_bot_status.json").read_text())
    assert state == {"description": "squeezed: 18%, user: 16%", "message_id": 42}


def test_update_bot_status_edits_existing_pinned_message(tmp_path, monkeypatch):
    _bot_status_env(tmp_path, monkeypatch)
    state_path = tmp_path / "state" / "telegram_bot_status.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"description": "squeezed: 10%, user: 16%", "message_id": 42}))
    calls = []
    monkeypatch.setattr(
        telegram_lib, "_call_telegram",
        lambda cfg, method, params, timeout=10: calls.append((method, params)) or {"ok": True},
    )

    telegram_lib.update_bot_status(_cfg())

    assert calls == [("editMessageText", {
        "chat_id": "111", "message_id": 42, "text": "squeezed: 18%, user: 16%",
    })]


def test_update_bot_status_skips_calls_when_nothing_changed(tmp_path, monkeypatch):
    _bot_status_env(tmp_path, monkeypatch)
    state_path = tmp_path / "state" / "telegram_bot_status.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"description": "squeezed: 18%, user: 16%", "message_id": 42}))
    calls = []
    monkeypatch.setattr(
        telegram_lib, "_call_telegram",
        lambda cfg, method, params, timeout=10: calls.append((method, params)) or {"ok": True},
    )

    telegram_lib.update_bot_status(_cfg())

    assert calls == []
