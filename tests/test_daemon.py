"""Tests for the pure, dependency-free helpers in daemon/daemon.py — command
construction, TODO scanning, progress fingerprinting, and Telegram command
classification. The actual I/O loops (Telegram long-poll, subprocess spawn,
timers) aren't unit tested here, matching this repo's existing convention of
testing logic modules directly and leaving thin process/glue code (formerly
bin/orchestrator.py, bin/telegram_bridge.py — neither had tests) uncovered."""
import importlib.util
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


def _decide_recheck(is_night=False, todos_changed=True, already_asked_for_current_signature=False):
    return daemon_mod.decide_paused_recheck_action(
        is_night=is_night,
        todos_changed=todos_changed,
        already_asked_for_current_signature=already_asked_for_current_signature,
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
