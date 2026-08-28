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


def test_send_message_prepends_hud_status_line_by_default(monkeypatch):
    captured = _sent_request(monkeypatch)
    monkeypatch.setattr(telegram_lib.hud_status, "current_status_line", lambda **kw: "🍋 hud line")

    telegram_lib.send_message("hello", _cfg())

    text = _sent_text(captured)
    assert text.startswith("🍋 hud line\n\n")
    assert text.endswith("hello")


def test_send_message_requests_plain_bar_since_telegram_cant_render_ansi(monkeypatch):
    """Telegram sends plain text and mangles raw ANSI escapes into literal
    garbage rather than interpreting them, so the HUD header must be built
    with color=False."""
    captured = _sent_request(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_lib.hud_status, "current_status_line",
        lambda **kw: calls.append(kw) or "🍋 hud line",
    )

    telegram_lib.send_message("hello", _cfg())

    assert calls == [{"color": False}]


def test_send_message_include_hud_false_skips_header(monkeypatch):
    captured = _sent_request(monkeypatch)
    monkeypatch.setattr(telegram_lib.hud_status, "current_status_line", lambda **kw: "🍋 hud line")

    telegram_lib.send_message("hello", _cfg(), include_hud=False)

    assert _sent_text(captured) == "hello"


def test_send_message_falls_back_to_plain_text_if_hud_status_errors(monkeypatch):
    captured = _sent_request(monkeypatch)

    def boom(**kw):
        raise RuntimeError("broken state file")

    monkeypatch.setattr(telegram_lib.hud_status, "current_status_line", boom)

    telegram_lib.send_message("hello", _cfg())

    assert _sent_text(captured) == "hello"
