"""Tests for the pure, dependency-free helpers in daemon/daemon.py — command
construction, TODO scanning, progress fingerprinting, and Telegram command
classification. The actual I/O loops (Telegram long-poll, subprocess spawn,
timers) aren't unit tested here, matching this repo's existing convention of
testing logic modules directly and leaving thin process/glue code (formerly
bin/orchestrator.py, bin/telegram_bridge.py — neither had tests) uncovered."""
import importlib.util
import json
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("daemon_mod", SQUEEZER_DIR / "daemon" / "daemon.py")
daemon_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon_mod)


# --- build_claude_command ---

def test_build_claude_command_fresh_session_no_resume():
    cmd = daemon_mod.build_claude_command("hello", None, ["/proj/a"])
    assert "--resume" not in cmd
    assert cmd[:3] == ["claude", "-p", "hello"]
    assert "--add-dir" in cmd and "/proj/a" in cmd


def test_build_claude_command_resumes_existing_session():
    cmd = daemon_mod.build_claude_command("hello", "sess-123", [])
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "sess-123"


def test_build_claude_command_adds_all_projects():
    cmd = daemon_mod.build_claude_command("hi", None, ["/a", "/b", "/c"])
    assert cmd.count("--add-dir") == 3
    for p in ("/a", "/b", "/c"):
        assert p in cmd


def test_build_claude_command_uses_permission_mode_auto_and_json_output():
    cmd = daemon_mod.build_claude_command("hi", None, [])
    assert "--permission-mode" in cmd and "auto" in cmd
    assert "--output-format" in cmd and "json" in cmd


# --- classify_command ---

def test_classify_pause_variants():
    assert daemon_mod.classify_command("/pause") == daemon_mod.TelegramCommand.PAUSE
    assert daemon_mod.classify_command("/stop") == daemon_mod.TelegramCommand.PAUSE
    assert daemon_mod.classify_command("  /PAUSE  ") == daemon_mod.TelegramCommand.PAUSE


def test_classify_resume_variants():
    assert daemon_mod.classify_command("/resume") == daemon_mod.TelegramCommand.RESUME
    assert daemon_mod.classify_command("/start") == daemon_mod.TelegramCommand.RESUME
    assert daemon_mod.classify_command("/continue") == daemon_mod.TelegramCommand.RESUME


def test_classify_mode_switch_commands():
    assert daemon_mod.classify_command("/auto") == daemon_mod.TelegramCommand.AUTO
    assert daemon_mod.classify_command("/manual") == daemon_mod.TelegramCommand.MANUAL
    assert daemon_mod.classify_command("/human") == daemon_mod.TelegramCommand.MANUAL


def test_classify_ordinary_text_is_message():
    assert daemon_mod.classify_command("please work on the AAPL task") == daemon_mod.TelegramCommand.MESSAGE
    assert daemon_mod.classify_command("2, cap it at 40%") == daemon_mod.TelegramCommand.MESSAGE


# --- compose_ack_message ---

def test_compose_ack_message_busy():
    msg = daemon_mod.compose_ack_message(busy=True)
    assert "Got it" in msg


def test_compose_ack_message_idle():
    msg = daemon_mod.compose_ack_message(busy=False)
    assert "Got it" in msg


def test_compose_ack_message_busy_and_idle_differ():
    assert daemon_mod.compose_ack_message(busy=True) != daemon_mod.compose_ack_message(busy=False)


# --- open_todo_summaries ---

def test_open_todo_summaries_collects_unchecked_items(tmp_path):
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "TODO.md").write_text(
        "# TODO\n- [ ] Fix the login bug\n- [x] Done already\n- [ ] Add retry logic\n"
    )
    items = daemon_mod.open_todo_summaries(tmp_path)
    assert "Fix the login bug" in items
    assert "Add retry logic" in items
    assert not any("Done already" in i for i in items)


def test_open_todo_summaries_skips_blocked_items(tmp_path):
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "TODO.md").write_text(
        "- [ ] Open item\n- [b] Blocked awaiting reply\n"
    )
    items = daemon_mod.open_todo_summaries(tmp_path)
    assert items == ["Open item"]


def test_open_todo_summaries_respects_max_items(tmp_path):
    (tmp_path / "acme").mkdir()
    lines = "\n".join(f"- [ ] item {i}" for i in range(10))
    (tmp_path / "acme" / "TODO.md").write_text(lines)
    items = daemon_mod.open_todo_summaries(tmp_path, max_items=3)
    assert len(items) == 3


def test_open_todo_summaries_empty_when_no_files(tmp_path):
    assert daemon_mod.open_todo_summaries(tmp_path) == []


# --- progress_signature ---

def test_progress_signature_changes_when_todo_changes(tmp_path):
    todos_dir = tmp_path / "todos"
    todos_dir.mkdir()
    worklog = tmp_path / "worklog.md"
    worklog.write_text("log v1")
    (todos_dir / "TODO.md").write_text("- [ ] a")

    sig1 = daemon_mod.progress_signature(worklog, todos_dir)
    (todos_dir / "TODO.md").write_text("- [x] a")
    sig2 = daemon_mod.progress_signature(worklog, todos_dir)
    assert sig1 != sig2


def test_progress_signature_stable_when_nothing_changes(tmp_path):
    todos_dir = tmp_path / "todos"
    todos_dir.mkdir()
    worklog = tmp_path / "worklog.md"
    worklog.write_text("log v1")
    (todos_dir / "TODO.md").write_text("- [ ] a")

    assert daemon_mod.progress_signature(worklog, todos_dir) == daemon_mod.progress_signature(worklog, todos_dir)


def test_progress_signature_handles_missing_files(tmp_path):
    todos_dir = tmp_path / "todos"
    todos_dir.mkdir()
    worklog = tmp_path / "worklog.md"  # doesn't exist
    assert isinstance(daemon_mod.progress_signature(worklog, todos_dir), str)


# --- todos_signature ---

def test_todos_signature_changes_when_todo_changes(tmp_path):
    todos_dir = tmp_path / "todos"
    todos_dir.mkdir()
    (todos_dir / "TODO.md").write_text("- [ ] a")

    sig1 = daemon_mod.todos_signature(todos_dir)
    (todos_dir / "TODO.md").write_text("- [ ] a\n- [ ] a new item")
    sig2 = daemon_mod.todos_signature(todos_dir)
    assert sig1 != sig2


def test_todos_signature_ignores_worklog_content(tmp_path):
    """Unlike progress_signature, todos_signature must NOT change just
    because worklog.md changed — paused_recheck_loop only cares about new
    *work* (todos/), not "a turn happened and left a note"."""
    todos_dir = tmp_path / "todos"
    todos_dir.mkdir()
    (todos_dir / "TODO.md").write_text("- [ ] a")
    worklog = tmp_path / "worklog.md"
    worklog.write_text("entry 1")

    sig1 = daemon_mod.todos_signature(todos_dir)
    worklog.write_text("entry 1\nentry 2 — a completely different worklog entry")
    sig2 = daemon_mod.todos_signature(todos_dir)
    assert sig1 == sig2


def test_todos_signature_handles_missing_dir(tmp_path):
    assert isinstance(daemon_mod.todos_signature(tmp_path / "nonexistent"), str)


# --- decide_paused_recheck_action ---

PausedRecheckAction = daemon_mod.PausedRecheckAction

_NOW = datetime(2026, 8, 29, 12, 0, 0)  # arbitrary fixed "daytime" instant


def _decide_recheck(
    now=_NOW, is_night=False, todos_changed=True,
    already_asked_for_current_signature=False, snoozed_until=None,
):
    return daemon_mod.decide_paused_recheck_action(
        now=now,
        is_night=is_night,
        todos_changed=todos_changed,
        already_asked_for_current_signature=already_asked_for_current_signature,
        snoozed_until=snoozed_until,
    )


def test_nothing_changed_stays_paused_regardless_of_time_of_day():
    assert _decide_recheck(todos_changed=False, is_night=True) == PausedRecheckAction.STAY_PAUSED
    assert _decide_recheck(todos_changed=False, is_night=False) == PausedRecheckAction.STAY_PAUSED


def test_changed_todos_at_night_auto_resumes():
    assert _decide_recheck(todos_changed=True, is_night=True) == PausedRecheckAction.AUTO_RESUME


def test_changed_todos_during_day_asks():
    assert _decide_recheck(todos_changed=True, is_night=False) == PausedRecheckAction.ASK


def test_changed_todos_during_day_already_asked_stays_paused():
    """Don't re-send the same "new work appeared" ask every 5 minutes for
    the same unchanged-since-asking todos content."""
    assert _decide_recheck(
        todos_changed=True, is_night=False, already_asked_for_current_signature=True,
    ) == PausedRecheckAction.STAY_PAUSED


def test_changed_todos_during_day_already_asked_but_changed_again_asks():
    """A *further* change after the last ask (already_asked_for_current_signature
    compares against the *current* signature, which the caller recomputes
    each tick) should ask again rather than staying silently paused forever."""
    assert _decide_recheck(
        todos_changed=True, is_night=False, already_asked_for_current_signature=False,
    ) == PausedRecheckAction.ASK


def test_active_snooze_suppresses_the_daytime_ask():
    """After a human declines ("no, check me in 2 hours"), no further ask
    (or spam) until the snooze deadline passes — this is what actually
    stops the "check and disturb every 5 minutes" behavior being fixed."""
    assert _decide_recheck(
        now=_NOW, snoozed_until=_NOW + timedelta(hours=2),
        todos_changed=True, is_night=False,
    ) == PausedRecheckAction.STAY_PAUSED


def test_expired_snooze_asks_again_even_if_already_asked_for_this_signature():
    """Once the snooze deadline passes, ask again — re-silencing because
    "already_asked_for_current_signature" is still true would defeat the
    entire point of letting the human pick a check-back time."""
    assert _decide_recheck(
        now=_NOW, snoozed_until=_NOW - timedelta(minutes=1),
        todos_changed=True, is_night=False, already_asked_for_current_signature=True,
    ) == PausedRecheckAction.ASK


def test_snooze_does_not_block_nighttime_auto_resume():
    """A daytime "don't disturb me" snooze says nothing about whether
    overnight auto-resume is welcome — night-time autonomy is a stronger,
    pre-existing guarantee and the two aren't in tension."""
    assert _decide_recheck(
        now=_NOW, snoozed_until=_NOW + timedelta(hours=2),
        todos_changed=True, is_night=True,
    ) == PausedRecheckAction.AUTO_RESUME


# --- TOTP / elevation state persistence ---

def test_load_totp_state_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    state = daemon_mod.load_totp_state()
    assert state == {"last_used_step": None, "failed_attempts": [], "locked_until": None}


def test_save_and_load_totp_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_totp_state({"last_used_step": 12345, "failed_attempts": [1.0, 2.0], "locked_until": None})
    assert daemon_mod.load_totp_state() == {"last_used_step": 12345, "failed_attempts": [1.0, 2.0], "locked_until": None}


def test_load_elevation_state_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    assert daemon_mod.load_elevation_state() == {"expires_at": None}


def test_save_and_load_elevation_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_elevation_state({"expires_at": "2026-09-04T18:00:00+00:00"})
    assert daemon_mod.load_elevation_state() == {"expires_at": "2026-09-04T18:00:00+00:00"}


def test_load_totp_state_recovers_from_corrupt_file(tmp_path, monkeypatch):
    """Simulate a daemon crash mid-write leaving a truncated/corrupt JSON file.
    load_totp_state should gracefully fall back to defaults rather than crash."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    # Create state dir and write garbage/truncated JSON
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "totp.json").write_text("{invalid json truncated")
    state = daemon_mod.load_totp_state()
    assert state == {"last_used_step": None, "failed_attempts": [], "locked_until": None}


def test_load_elevation_state_recovers_from_corrupt_file(tmp_path, monkeypatch):
    """Simulate a daemon crash mid-write leaving a truncated/corrupt JSON file.
    load_elevation_state should gracefully fall back to defaults rather than crash."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    # Create state dir and write garbage/truncated JSON
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "elevation.json").write_text("{incomplete")
    state = daemon_mod.load_elevation_state()
    assert state == {"expires_at": None}


# --- build_claude_command settings overlay ---

def test_build_claude_command_without_overlay_omits_settings_flag():
    cmd = daemon_mod.build_claude_command("hi", None, [])
    assert "--settings" not in cmd


def test_build_claude_command_with_overlay_appends_settings_flag():
    cmd = daemon_mod.build_claude_command("hi", None, [], settings_overlay_path="/tmp/overlay.json")
    assert "--settings" in cmd
    assert cmd[cmd.index("--settings") + 1] == "/tmp/overlay.json"


# --- current_elevation_overlay_path ---

def test_current_elevation_overlay_path_none_when_no_elevation(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    assert daemon_mod.current_elevation_overlay_path(now=datetime(2026, 9, 4, 12, 0, 0)) is None


def test_current_elevation_overlay_path_none_when_expired(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_elevation_state({"expires_at": "2026-09-04T10:00:00+00:00"})
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    assert daemon_mod.current_elevation_overlay_path(now=now) is None


def test_current_elevation_overlay_path_writes_overlay_when_active(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_elevation_state({"expires_at": "2026-09-04T18:00:00+00:00"})
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    path = daemon_mod.current_elevation_overlay_path(now=now)
    assert path is not None
    content = json.loads(Path(path).read_text())
    assert content["autoMode"]["allow"]
    assert "hard_deny" in content["autoMode"]["allow"][-1]


def test_current_elevation_overlay_path_none_when_expires_at_malformed(tmp_path, monkeypatch):
    """A malformed/non-ISO expires_at must fail safe (treated as no active
    elevation), not raise — a crash here would take down every claude -p
    turn spawn in spawn_claude, which doesn't wrap this call in try/except."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_elevation_state({"expires_at": "not-a-timestamp"})
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    assert daemon_mod.current_elevation_overlay_path(now=now) is None


def test_current_elevation_overlay_path_active_with_naive_now(tmp_path, monkeypatch):
    """A naive (no tzinfo) now must be normalized to UTC and compared
    correctly against a tz-aware active expires_at."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_elevation_state({"expires_at": "2026-09-04T18:00:00+00:00"})
    now = datetime(2026, 9, 4, 12, 0, 0)  # naive
    path = daemon_mod.current_elevation_overlay_path(now=now)
    assert path is not None


# --- classify_command: elevate / lockdown ---

def test_classify_elevate_command():
    assert daemon_mod.classify_command("/elevate 123456 8") == daemon_mod.TelegramCommand.ELEVATE


def test_classify_elevate_with_bad_args_is_still_elevate():
    # classify_command only recognizes the shape; totp.parse_elevate_command
    # (already tested in test_totp.py) is what validates code/hours.
    assert daemon_mod.classify_command("/elevate nonsense") == daemon_mod.TelegramCommand.ELEVATE


def test_classify_lockdown_command():
    assert daemon_mod.classify_command("/lockdown") == daemon_mod.TelegramCommand.LOCKDOWN


def test_classify_ordinary_text_still_message_after_elevate_added():
    assert daemon_mod.classify_command("elevate my mood please") == daemon_mod.TelegramCommand.MESSAGE


# --- _handle_telegram_message: /elevate and /lockdown wiring ---
#
# telegram_lib.send_message is stubbed in every test below so no real
# Telegram API call is made; since it's stubbed, `cfg` is never actually
# used for anything but being forwarded into that stub, so a plain None
# stands in for a real telegram_lib.TelegramConfig().

def test_elevate_locked_out_rejects_without_verifying(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.setattr(daemon_mod.telegram_lib, "send_message", lambda *a, **k: None)
    now = time.time()
    locked_state = {"last_used_step": None, "failed_attempts": [], "locked_until": now + 900}
    daemon_mod.save_totp_state(locked_state)

    daemon_mod._handle_telegram_message(
        "/elevate 123456 8", None, queue.Queue(), threading.Event()
    )

    after = daemon_mod.load_totp_state()
    # unchanged: the lockout check short-circuited before verify_code/
    # record_failed_attempt were ever reached.
    assert after["failed_attempts"] == []
    assert after["locked_until"] == locked_state["locked_until"]
    assert after["last_used_step"] is None
    assert daemon_mod.load_elevation_state()["expires_at"] is None


def test_elevate_failed_verify_persists_failed_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.setenv("TOTP_SECRET", daemon_mod.totp.generate_secret())
    monkeypatch.setattr(daemon_mod.telegram_lib, "send_message", lambda *a, **k: None)
    before = daemon_mod.load_totp_state()
    assert before["failed_attempts"] == []

    daemon_mod._handle_telegram_message(
        "/elevate 000000 8", None, queue.Queue(), threading.Event()
    )

    after = daemon_mod.load_totp_state()
    assert len(after["failed_attempts"]) == len(before["failed_attempts"]) + 1
    assert daemon_mod.load_elevation_state()["expires_at"] is None


def test_elevate_success_persists_last_used_step_and_creates_elevation(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    secret = daemon_mod.totp.generate_secret()
    monkeypatch.setenv("TOTP_SECRET", secret)
    monkeypatch.setattr(daemon_mod.telegram_lib, "send_message", lambda *a, **k: None)
    now = time.time()
    code = daemon_mod.totp.totp_at_step(secret, daemon_mod.totp.current_step(now))

    daemon_mod._handle_telegram_message(
        f"/elevate {code} 8", None, queue.Queue(), threading.Event()
    )

    totp_state = daemon_mod.load_totp_state()
    assert totp_state["last_used_step"] is not None

    elevation = daemon_mod.load_elevation_state()
    assert elevation["expires_at"] is not None
    expires_at = datetime.fromisoformat(elevation["expires_at"])
    expected = datetime.now(timezone.utc) + timedelta(hours=8)
    assert abs((expires_at - expected).total_seconds()) < 60


def test_lockdown_clears_elevation(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.setattr(daemon_mod.telegram_lib, "send_message", lambda *a, **k: None)
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    daemon_mod.save_elevation_state({"expires_at": future_iso})

    daemon_mod._handle_telegram_message(
        "/lockdown", None, queue.Queue(), threading.Event()
    )

    assert daemon_mod.load_elevation_state() == {"expires_at": None}
