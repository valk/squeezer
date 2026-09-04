"""Minimal tests for the no_reserve_hours feature and weekly pacing (smart
mode) in daemon/usage_lib.py."""
import importlib.util
import json
import subprocess
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("usage_lib", SQUEEZER_DIR / "daemon" / "usage_lib.py")
usage_lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usage_lib)


@pytest.fixture
def config_json(tmp_path, monkeypatch):
    """Point usage_lib (via daemon/config.py) at a scratch SQUEEZER_HOME
    instead of the real one."""
    def _write(data):
        monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data))
        return path
    return _write


def test_default_window_when_no_config_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))  # no config.json here
    assert usage_lib.is_within_no_reserve_hours(now=time(4, 0))
    assert not usage_lib.is_within_no_reserve_hours(now=time(12, 0))


def test_default_window_when_key_omitted(config_json):
    config_json({"reserve_percent": 20})
    assert usage_lib.is_within_no_reserve_hours(now=time(2, 0))  # inclusive start
    assert usage_lib.is_within_no_reserve_hours(now=time(6, 59))
    assert not usage_lib.is_within_no_reserve_hours(now=time(7, 0))  # exclusive end


def test_custom_window(config_json):
    config_json({"no_reserve_hours": {"start": "01:00", "end": "03:00"}})
    assert usage_lib.is_within_no_reserve_hours(now=time(1, 30))
    assert not usage_lib.is_within_no_reserve_hours(now=time(4, 0))


def test_window_wraps_past_midnight(config_json):
    config_json({"no_reserve_hours": {"start": "22:00", "end": "06:00"}})
    assert usage_lib.is_within_no_reserve_hours(now=time(23, 0))
    assert usage_lib.is_within_no_reserve_hours(now=time(3, 0))
    assert not usage_lib.is_within_no_reserve_hours(now=time(12, 0))


def test_explicit_null_disables_window(config_json):
    config_json({"no_reserve_hours": None})
    assert not usage_lib.is_within_no_reserve_hours(now=time(4, 0))


def test_reserve_percent_zero_inside_window(monkeypatch, config_json):
    config_json({"reserve_percent": 20})
    monkeypatch.setattr(usage_lib, "is_within_no_reserve_hours", lambda: True)
    assert usage_lib.load_reserve_percent() == 0


def test_reserve_percent_normal_outside_window(monkeypatch, config_json):
    config_json({"reserve_percent": 20})
    monkeypatch.setattr(usage_lib, "is_within_no_reserve_hours", lambda: False)
    assert usage_lib.load_reserve_percent() == 20


# --- Weekly pacing / smart-mode gate ---

@pytest.fixture
def weekly_state_path(tmp_path, monkeypatch):
    path = tmp_path / "weekly_budget.json"
    monkeypatch.setattr(usage_lib, "WEEKLY_STATE_PATH", path)
    return path


def test_weekly_pace_ratio_none_when_uncalibrated(weekly_state_path):
    assert usage_lib.weekly_pace_ratio() is None


def test_load_weekly_state_reinitializes_on_corrupt_file(weekly_state_path):
    # e.g. a process killed mid-write, leaving a 0-byte file behind.
    weekly_state_path.write_text("")
    state = usage_lib.load_weekly_state()
    assert state["calibrated"] is False
    assert weekly_state_path.read_text().strip()  # reinitialized on disk, not left empty


def test_load_state_reinitializes_on_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    state_path = tmp_path / "state" / "window_budget.json"
    monkeypatch.setattr(usage_lib, "STATE_PATH", state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("")
    state = usage_lib.load_state()
    assert state["calibrated"] is False
    assert state_path.read_text().strip()


def test_calibrate_week_computes_reset_ts(weekly_state_path):
    usage_lib.cmd_calibrate_week(40, 96)
    state = usage_lib.load_weekly_state()
    assert state["calibrated"] is True
    assert state["last_calibrated_percent"] == 40
    reset_ts = datetime.fromisoformat(state["reset_ts"])
    expected = datetime.now(timezone.utc) + timedelta(hours=96)
    assert abs((reset_ts - expected).total_seconds()) < 5


def test_calibrate_week_rejects_bad_percent(weekly_state_path):
    with pytest.raises(SystemExit):
        usage_lib.cmd_calibrate_week(150, 96)


def test_calibrate_week_rejects_negative_hours(weekly_state_path):
    with pytest.raises(SystemExit):
        usage_lib.cmd_calibrate_week(40, -1)


def _set_weekly_state(path, percent, hours_until_reset):
    state = {
        "last_calibrated_percent": percent,
        "reset_ts": (datetime.now(timezone.utc) + timedelta(hours=hours_until_reset)).isoformat(),
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "calibrated": True,
    }
    path.write_text(json.dumps(state))


def test_weekly_pace_ratio_on_pace(weekly_state_path):
    # 50% used, 50% of the week elapsed (84h remaining of 168h) -> ratio ~1.0
    _set_weekly_state(weekly_state_path, 50, 84)
    assert usage_lib.weekly_pace_ratio() == pytest.approx(1.0, abs=0.02)


def test_weekly_pace_ratio_behind_pace(weekly_state_path):
    # 10% used, 50% elapsed -> well behind pace (ratio ~0.2)
    _set_weekly_state(weekly_state_path, 10, 84)
    assert usage_lib.weekly_pace_ratio() == pytest.approx(0.2, abs=0.02)


def test_weekly_pace_ratio_ahead_of_pace(weekly_state_path):
    # 90% used, 50% elapsed -> well ahead of pace (ratio ~1.8)
    _set_weekly_state(weekly_state_path, 90, 84)
    assert usage_lib.weekly_pace_ratio() == pytest.approx(1.8, abs=0.05)


def test_weekly_pace_ratio_no_divide_by_zero_freshly_calibrated(weekly_state_path):
    # Reset is a full week away (elapsed ~0) -> clamped, must not raise/inf.
    _set_weekly_state(weekly_state_path, 5, 168)
    ratio = usage_lib.weekly_pace_ratio()
    assert ratio is not None and ratio > 0


def test_smart_mode_gate_fail_open_when_uncalibrated(weekly_state_path):
    result = usage_lib.smart_mode_gate(tasks_per_cycle=3)
    assert result == {"proceed": True, "tasks_this_cycle": 3, "ratio": None}


def test_smart_mode_gate_behind_pace_full_allotment(weekly_state_path):
    _set_weekly_state(weekly_state_path, 10, 84)  # ratio ~0.2
    result = usage_lib.smart_mode_gate(tasks_per_cycle=4)
    assert result["proceed"] is True
    assert result["tasks_this_cycle"] == 4


def test_smart_mode_gate_on_pace_half_allotment(weekly_state_path):
    _set_weekly_state(weekly_state_path, 50, 84)  # ratio ~1.0
    result = usage_lib.smart_mode_gate(tasks_per_cycle=4)
    assert result["proceed"] is True
    assert result["tasks_this_cycle"] == 2


def test_smart_mode_gate_ahead_of_pace_skips(weekly_state_path):
    _set_weekly_state(weekly_state_path, 90, 84)  # ratio ~1.8
    result = usage_lib.smart_mode_gate(tasks_per_cycle=4)
    assert result["proceed"] is False
    assert "reason" in result


def test_smart_mode_gate_half_allotment_never_zero(weekly_state_path):
    _set_weekly_state(weekly_state_path, 50, 84)  # ratio ~1.0 -> half of 1 would be 0
    result = usage_lib.smart_mode_gate(tasks_per_cycle=1)
    assert result["tasks_this_cycle"] == 1


def test_load_smart_mode_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))  # no config.json here
    config = usage_lib.load_smart_mode_config()
    assert config == {"enabled": True, "tasks_per_cycle": 3}


def test_cmd_quiet_hours_prints_current_state(monkeypatch, capsys):
    monkeypatch.setattr(usage_lib, "is_within_no_reserve_hours", lambda: True)
    usage_lib.cmd_quiet_hours()
    assert json.loads(capsys.readouterr().out) == {"quiet_hours": True}


def test_load_smart_mode_config_project_override(config_json):
    config_json({
        "smart_mode": {"enabled": True, "tasks_per_cycle": 3},
        "projects": [
            {"name": "acme", "path": "/tmp/acme", "smart_mode": {"tasks_per_cycle": 5}},
        ],
    })
    assert usage_lib.load_smart_mode_config("acme")["tasks_per_cycle"] == 5
    assert usage_lib.load_smart_mode_config("other-project")["tasks_per_cycle"] == 3


# --- Self-calibration: parse `claude -p "/usage"` and drive both trackers ---

@pytest.fixture
def window_state_path(tmp_path, monkeypatch):
    path = tmp_path / "window_budget.json"
    monkeypatch.setattr(usage_lib, "STATE_PATH", path)
    return path


class _FakeCompletedProcess:
    def __init__(self, stdout):
        self.stdout = stdout


# --- budget_ok / cmd_check ---

def test_budget_ok_fails_open_when_uncalibrated(window_state_path):
    assert usage_lib.budget_ok("/fake/transcript.jsonl") is True


def test_budget_ok_fails_open_with_no_transcript(window_state_path):
    assert usage_lib.budget_ok(None) is True


def test_budget_ok_false_when_reserve_breached(window_state_path, monkeypatch):
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
    })
    monkeypatch.setattr(usage_lib, "sum_usage_since", lambda path, ts: 900)  # 90% used
    monkeypatch.setattr(usage_lib, "load_reserve_percent", lambda: 20)  # threshold at 80%
    assert usage_lib.budget_ok("/fake/transcript.jsonl") is False


def test_budget_ok_true_when_under_threshold(window_state_path, monkeypatch):
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
    })
    monkeypatch.setattr(usage_lib, "sum_usage_since", lambda path, ts: 500)  # 50% used
    monkeypatch.setattr(usage_lib, "load_reserve_percent", lambda: 20)  # threshold at 80%
    assert usage_lib.budget_ok("/fake/transcript.jsonl") is True


def test_cmd_check_allows_when_no_transcript_path(window_state_path, monkeypatch, capsys):
    import io
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps({})))
    usage_lib.cmd_check()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_cmd_check_rolls_an_overdue_window_before_checking(window_state_path, monkeypatch, capsys):
    """Regression: window rollover previously only ever happened on the
    daemon's own 20-minute self_calibrate_loop timer, so a window left stale
    by a dead/missing daemon (see daemon/hud_status.py's "squeezed: 11%
    despite squeezer not having run this window" bug) stayed stale until the
    daemon came back AND ticked again. This hook fires on every tool call in
    every session regardless of daemon uptime, so it must self-heal a
    window overdue for a roll before computing anything against it."""
    import io
    stale_start = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    usage_lib.save_state({
        "window_start_ts": stale_start,
        "estimated_window_total": usage_lib.DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": True,
        "squeezer_transcript_paths": ["/fake/old-window-squeezer-turn.jsonl"],
    })
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps({})))
    usage_lib.cmd_check()
    state = usage_lib.load_state()
    assert state["window_start_ts"] != stale_start
    assert state["squeezer_transcript_paths"] == []


def test_cmd_check_denies_when_reserve_breached_for_squeezer_cwd(window_state_path, monkeypatch, capsys, tmp_path):
    """The reserve only gates squeezer's own daemon-spawned turns (cwd ==
    squeezer_home(), see daemon.spawn_claude)."""
    import io
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
    })
    monkeypatch.setattr(usage_lib, "sum_usage_since", lambda path, ts: 900)
    monkeypatch.setattr(usage_lib, "load_reserve_percent", lambda: 20)
    payload = {"transcript_path": "/fake/transcript.jsonl", "cwd": str(tmp_path)}
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_cmd_check_allows_reserve_breached_for_non_squeezer_cwd(window_state_path, monkeypatch, capsys, tmp_path):
    """An interactive session or an agent working on some other project must
    never be denied a tool call by squeezer's own budget — this is the
    "budget guard blocks everyone" edge case being fixed."""
    import io
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
    })
    monkeypatch.setattr(usage_lib, "sum_usage_since", lambda path, ts: 900)
    monkeypatch.setattr(usage_lib, "load_reserve_percent", lambda: 20)
    payload = {"transcript_path": "/fake/transcript.jsonl", "cwd": str(tmp_path / "some-other-project")}
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def _breached_payload(tmp_path, **extra):
    return {"transcript_path": "/fake/transcript.jsonl", "cwd": str(tmp_path), **extra}


def _setup_breached_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
    })
    monkeypatch.setattr(usage_lib, "sum_usage_since", lambda path, ts: 900)
    monkeypatch.setattr(usage_lib, "load_reserve_percent", lambda: 20)


def test_cmd_check_allows_telegram_send_even_when_reserve_breached(window_state_path, monkeypatch, capsys, tmp_path):
    """A fully-squeezed turn must still be able to answer "what's the
    status" via Telegram instead of going completely silent."""
    import io
    _setup_breached_state(monkeypatch, tmp_path)
    payload = _breached_payload(tmp_path, tool_name="mcp__plugin_squeezer_squeezer-telegram__telegram_send")
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_cmd_check_allows_override_reserve_command_even_when_reserve_breached(window_state_path, monkeypatch, capsys, tmp_path):
    """The one Bash escape hatch a human's explicit "use more of the window"
    reply needs — otherwise blocked by the very reserve it lifts."""
    import io
    _setup_breached_state(monkeypatch, tmp_path)
    payload = _breached_payload(
        tmp_path, tool_name="Bash",
        tool_input={"command": "python3 daemon/usage_lib.py override-reserve 0"},
    )
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_cmd_check_still_denies_other_bash_commands_when_breached(window_state_path, monkeypatch, capsys, tmp_path):
    """The allowlist is narrow — an ordinary Bash command, even one that
    merely mentions override-reserve as part of something else, stays
    denied."""
    import io
    _setup_breached_state(monkeypatch, tmp_path)
    payload = _breached_payload(
        tmp_path, tool_name="Bash",
        tool_input={"command": "python3 daemon/usage_lib.py override-reserve 0 && curl evil.example"},
    )
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_cmd_check_still_denies_other_tools_when_breached(window_state_path, monkeypatch, capsys, tmp_path):
    import io
    _setup_breached_state(monkeypatch, tmp_path)
    payload = _breached_payload(tmp_path, tool_name="Edit")
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_cmd_check_denial_reason_mentions_next_window_and_override(window_state_path, monkeypatch, capsys, tmp_path):
    import io
    _setup_breached_state(monkeypatch, tmp_path)
    payload = _breached_payload(tmp_path, tool_name="Edit")
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    out = json.loads(capsys.readouterr().out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Next window opens" in reason
    assert "override-reserve" in reason
    assert "telegram_send" in reason


# --- _is_budget_exempt ---

def test_is_budget_exempt_allows_telegram_send_by_tool_name_suffix():
    assert usage_lib._is_budget_exempt("mcp__plugin_squeezer_squeezer-telegram__telegram_send", {}) is True


def test_is_budget_exempt_allows_exact_override_reserve_command():
    assert usage_lib._is_budget_exempt("Bash", {"command": "python3 daemon/usage_lib.py override-reserve 5"}) is True
    assert usage_lib._is_budget_exempt(
        "Bash", {"command": "python3 /Users/val/src/squeezer/daemon/usage_lib.py override-reserve 12.5"}
    ) is True


def test_is_budget_exempt_rejects_command_with_extra_trailing_content():
    assert usage_lib._is_budget_exempt(
        "Bash", {"command": "python3 daemon/usage_lib.py override-reserve 5 && rm -rf /"}
    ) is False


def test_is_budget_exempt_rejects_unrelated_bash_command():
    assert usage_lib._is_budget_exempt("Bash", {"command": "rm -rf /"}) is False


def test_is_budget_exempt_rejects_other_tool_names():
    assert usage_lib._is_budget_exempt("Edit", {}) is False
    assert usage_lib._is_budget_exempt("Read", {}) is False


# --- load_reserve_percent override / cmd_override_reserve / roll_window clearing ---

def test_load_reserve_percent_returns_override_when_set(window_state_path):
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
        "reserve_override_percent": 3,
    })
    assert usage_lib.load_reserve_percent() == 3


def test_load_reserve_percent_falls_back_to_config_without_override(window_state_path, config_json, monkeypatch):
    config_json({"reserve_percent": 33})
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
    })
    monkeypatch.setattr(usage_lib, "is_within_no_reserve_hours", lambda *a, **k: False)
    assert usage_lib.load_reserve_percent() == 33


def test_cmd_override_reserve_persists_to_state(window_state_path, capsys):
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
    })
    usage_lib.cmd_override_reserve(7)
    assert usage_lib.load_state()["reserve_override_percent"] == 7
    assert "7%" in capsys.readouterr().out


def test_roll_window_clears_reserve_override(window_state_path):
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
        "reserve_override_percent": 5,
    })
    usage_lib.roll_window()
    assert "reserve_override_percent" not in usage_lib.load_state()


def test_is_squeezer_cwd_matches_squeezer_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    assert usage_lib._is_squeezer_cwd(str(tmp_path)) is True


def test_is_squeezer_cwd_rejects_other_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    assert usage_lib._is_squeezer_cwd(str(tmp_path / "other")) is False


def test_is_squeezer_cwd_handles_none():
    assert usage_lib._is_squeezer_cwd(None) is False


def test_cmd_check_tracks_squeezer_transcript_path(window_state_path, monkeypatch, capsys, tmp_path):
    import io
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    payload = {"transcript_path": "/fake/squeezer-session.jsonl", "cwd": str(tmp_path)}
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    assert usage_lib.load_state()["squeezer_transcript_paths"] == ["/fake/squeezer-session.jsonl"]


def test_cmd_check_does_not_track_non_squeezer_transcript_path(window_state_path, monkeypatch, capsys, tmp_path):
    import io
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    payload = {"transcript_path": "/fake/human-session.jsonl", "cwd": str(tmp_path / "elsewhere")}
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    assert usage_lib.load_state().get("squeezer_transcript_paths", []) == []


def test_cmd_check_dedupes_squeezer_transcript_path(window_state_path, monkeypatch, capsys, tmp_path):
    import io
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    payload = {"transcript_path": "/fake/squeezer-session.jsonl", "cwd": str(tmp_path)}
    for _ in range(2):
        monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
        usage_lib.cmd_check()
    assert usage_lib.load_state()["squeezer_transcript_paths"] == ["/fake/squeezer-session.jsonl"]


def test_sum_squeezer_usage_since_sums_across_tracked_transcripts(window_state_path, tmp_path):
    now = datetime.now(timezone.utc)

    def _entry(tokens):
        return json.dumps({"timestamp": now.isoformat(), "message": {"usage": {"input_tokens": tokens}}})

    t1 = tmp_path / "t1.jsonl"
    t2 = tmp_path / "t2.jsonl"
    t1.write_text(_entry(100) + "\n")
    t2.write_text(_entry(50) + "\n")
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": usage_lib.DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": False,
        "squeezer_transcript_paths": [str(t1), str(t2)],
    })
    since = (now - timedelta(minutes=10)).isoformat()
    assert usage_lib.sum_squeezer_usage_since(since) == 150


def test_sum_squeezer_usage_since_empty_when_untracked(window_state_path):
    assert usage_lib.sum_squeezer_usage_since(usage_lib.now_iso()) == 0


# --- last_known_human_transcript_path / total_used_since ---
#
# Regression coverage for the bug where hud_status's "of used" bar jumped
# 0% -> 100% right after squeezer's first daemon-spawned turn, then climbed
# past 100% (e.g. 270%) as squeezer ran more turns: the old code used the
# single last_known_transcript_path (overwritten by *any* session's
# PreToolUse hook, squeezer's own turns included) as the "total usage"
# denominator, so once squeezer was the most recent turn, that denominator
# collapsed to squeezer's own (single, narrow) transcript — same or smaller
# than squeezer_used itself, which sums across *all* of squeezer's turns.

def test_cmd_check_tracks_last_known_human_transcript_path_for_non_squeezer_turn(
    window_state_path, monkeypatch, capsys, tmp_path
):
    import io
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    payload = {"transcript_path": "/fake/human-session.jsonl", "cwd": str(tmp_path / "elsewhere")}
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(payload)))
    usage_lib.cmd_check()
    assert usage_lib.load_state()["last_known_human_transcript_path"] == "/fake/human-session.jsonl"


def test_cmd_check_does_not_clobber_human_transcript_with_a_squeezer_turn(
    window_state_path, monkeypatch, capsys, tmp_path
):
    """A human session fires a tool call, then squeezer's own daemon-spawned
    turn fires one right after — last_known_human_transcript_path must keep
    pointing at the human session, not get overwritten by squeezer's."""
    import io
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))

    human_payload = {"transcript_path": "/fake/human-session.jsonl", "cwd": str(tmp_path / "elsewhere")}
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(human_payload)))
    usage_lib.cmd_check()

    squeezer_payload = {"transcript_path": "/fake/squeezer-session.jsonl", "cwd": str(tmp_path)}
    monkeypatch.setattr(usage_lib.sys, "stdin", io.StringIO(json.dumps(squeezer_payload)))
    usage_lib.cmd_check()

    state = usage_lib.load_state()
    assert state["last_known_human_transcript_path"] == "/fake/human-session.jsonl"
    # the generic (whoever-was-last) pointer is still allowed to move — only
    # the human-specific one needs to stay put.
    assert state["last_known_transcript_path"] == "/fake/squeezer-session.jsonl"


def test_total_used_since_sums_human_and_squeezer_transcripts(window_state_path, tmp_path):
    now = datetime.now(timezone.utc)

    def _entry(tokens):
        return json.dumps({"timestamp": now.isoformat(), "message": {"usage": {"input_tokens": tokens}}})

    human = tmp_path / "human.jsonl"
    squeezer1 = tmp_path / "squeezer1.jsonl"
    squeezer2 = tmp_path / "squeezer2.jsonl"
    human.write_text(_entry(1000) + "\n")
    squeezer1.write_text(_entry(100) + "\n")
    squeezer2.write_text(_entry(50) + "\n")

    state = {
        "window_start_ts": (now - timedelta(minutes=10)).isoformat(),
        "estimated_window_total": usage_lib.DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": True,
        "last_known_human_transcript_path": str(human),
        "squeezer_transcript_paths": [str(squeezer1), str(squeezer2)],
    }
    assert usage_lib.total_used_since(state) == 1150


def test_total_used_since_not_capped_by_a_single_squeezer_transcript(window_state_path, tmp_path):
    """The bug: squeezer's own turns each get a fresh transcript file, and
    squeezer_used sums across all of them (see sum_squeezer_usage_since) —
    total_used_since must too, rather than tracking only whichever single
    transcript a stale "last known" pointer happens to reference."""
    now = datetime.now(timezone.utc)

    def _entry(tokens):
        return json.dumps({"timestamp": now.isoformat(), "message": {"usage": {"input_tokens": tokens}}})

    human = tmp_path / "human.jsonl"
    squeezer_turns = [tmp_path / f"squeezer{i}.jsonl" for i in range(3)]
    human.write_text(_entry(100) + "\n")
    for path in squeezer_turns:
        path.write_text(_entry(90) + "\n")

    state = {
        "window_start_ts": (now - timedelta(minutes=10)).isoformat(),
        "estimated_window_total": usage_lib.DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": True,
        # simulates the old bug's stale generic pointer landing on just one
        # squeezer turn — total_used_since must not be fooled by it.
        "last_known_transcript_path": str(squeezer_turns[0]),
        "last_known_human_transcript_path": str(human),
        "squeezer_transcript_paths": [str(p) for p in squeezer_turns],
    }
    assert usage_lib.total_used_since(state) == 100 + 90 * 3


def test_cmd_roll_window_resets_squeezer_transcript_paths(window_state_path, capsys):
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": usage_lib.DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": False,
        "squeezer_transcript_paths": ["/fake/old.jsonl"],
    })
    usage_lib.cmd_roll_window()
    assert usage_lib.load_state()["squeezer_transcript_paths"] == []


def test_roll_window_returns_new_state(window_state_path):
    usage_lib.save_state({
        "window_start_ts": "2026-08-27T00:00:00+00:00",
        "estimated_window_total": usage_lib.DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": False,
        "squeezer_transcript_paths": ["/fake/old.jsonl"],
    })
    result = usage_lib.roll_window()
    assert result["window_start_ts"] != "2026-08-27T00:00:00+00:00"
    assert result["estimated_window_total"] == usage_lib.DEFAULT_ESTIMATE


# --- maybe_roll_window: automatic counterpart to the manual roll-window CLI
# — see its docstring for why this exists (nothing else ever detected a
# real window reset, so window_start_ts grew stale indefinitely) ---

def test_maybe_roll_window_noop_when_period_not_elapsed(window_state_path):
    start = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    usage_lib.save_state({
        "window_start_ts": start.isoformat(),
        "estimated_window_total": usage_lib.DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": True,
        "squeezer_transcript_paths": ["/fake/old.jsonl"],
    })
    result = usage_lib.maybe_roll_window(now=start + timedelta(hours=4, minutes=59))
    assert result is None
    assert usage_lib.load_state()["window_start_ts"] == start.isoformat()
    assert usage_lib.load_state()["squeezer_transcript_paths"] == ["/fake/old.jsonl"]


def test_maybe_roll_window_rolls_once_period_elapsed(window_state_path, monkeypatch):
    start = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    usage_lib.save_state({
        "window_start_ts": start.isoformat(),
        "estimated_window_total": usage_lib.DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": True,
        "squeezer_transcript_paths": ["/fake/old.jsonl"],
    })
    monkeypatch.setattr(usage_lib, "find_known_transcript_path", lambda: None)

    result = usage_lib.maybe_roll_window(now=start + timedelta(hours=5, minutes=1))

    assert result is not None
    assert result["window_start_ts"] != start.isoformat()
    state = usage_lib.load_state()
    assert state["window_start_ts"] == result["window_start_ts"]
    assert state["squeezer_transcript_paths"] == []


def test_maybe_roll_window_sums_last_known_transcript_into_history(window_state_path, monkeypatch, tmp_path):
    start = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    usage_lib.save_state({
        "window_start_ts": start.isoformat(),
        "estimated_window_total": 1000,
        "past_window_totals": [],
        "calibrated": True,
        "squeezer_transcript_paths": [],
    })
    transcript = tmp_path / "last.jsonl"
    transcript.write_text(json.dumps({
        "timestamp": (start + timedelta(hours=1)).isoformat(),
        "message": {"usage": {"input_tokens": 500}},
    }) + "\n")
    monkeypatch.setattr(usage_lib, "find_known_transcript_path", lambda: str(transcript))

    usage_lib.maybe_roll_window(now=start + timedelta(hours=5, minutes=1))

    assert usage_lib.load_state()["past_window_totals"] == [500]


def test_maybe_roll_window_noop_right_after_state_created(window_state_path):
    """load_state() seeds a brand-new window's window_start_ts to now — must
    not be treated as already-elapsed."""
    result = usage_lib.maybe_roll_window()
    assert result is None


def test_parse_usage_output_with_minutes():
    now = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)
    text = (
        "Current session: 15% used · resets Aug 23 at 4:09pm (Asia/Jerusalem)\n"
        "Current week (all models): 17% used · resets Aug 25 at 6:59am (Asia/Jerusalem)\n"
    )
    result = usage_lib.parse_usage_output(text, now=now)
    assert result["session_percent"] == 15.0
    assert result["week_percent"] == 17.0

    expected_session_reset = datetime(2026, 8, 23, 16, 9, tzinfo=ZoneInfo("Asia/Jerusalem"))
    expected_hours = (expected_session_reset.astimezone(timezone.utc) - now).total_seconds() / 3600
    assert result["session_hours_until_reset"] == pytest.approx(expected_hours, abs=0.01)

    expected_week_reset = datetime(2026, 8, 25, 6, 59, tzinfo=ZoneInfo("Asia/Jerusalem"))
    expected_week_hours = (expected_week_reset.astimezone(timezone.utc) - now).total_seconds() / 3600
    assert result["week_hours_until_reset"] == pytest.approx(expected_week_hours, abs=0.01)


def test_parse_usage_output_without_minutes():
    # Real /usage output omits ":00" (e.g. "7am" not "7:00am") — must still parse.
    now = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)
    text = (
        "Current session: 20% used · resets Aug 23 at 5pm (Asia/Jerusalem)\n"
        "Current week (all models): 20% used · resets Aug 25 at 7am (Asia/Jerusalem)\n"
    )
    result = usage_lib.parse_usage_output(text, now=now)
    assert result["session_percent"] == 20.0
    expected_session_reset = datetime(2026, 8, 23, 17, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    expected_hours = (expected_session_reset.astimezone(timezone.utc) - now).total_seconds() / 3600
    assert result["session_hours_until_reset"] == pytest.approx(expected_hours, abs=0.01)


def test_parse_usage_output_rolls_over_year():
    now = datetime(2026, 12, 30, 10, 0, tzinfo=timezone.utc)
    text = (
        "Current session: 5% used · resets Jan 2 at 9am (UTC)\n"
        "Current week (all models): 5% used · resets Jan 2 at 9am (UTC)\n"
    )
    result = usage_lib.parse_usage_output(text, now=now)
    assert 0 < result["session_hours_until_reset"] < 24 * 10  # positive, not a year away


def test_parse_usage_output_missing_line_raises():
    with pytest.raises(ValueError):
        usage_lib.parse_usage_output("no usage information in this text at all")


def test_parse_usage_output_session_at_zero_percent_omits_resets_clause():
    # Real /usage output drops "· resets ..." for the session line when it's
    # a fresh 5-hour window with 0% used (nothing to report a reset time
    # for yet) — must still parse the percent instead of raising.
    now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
    text = (
        "Current session: 0% used\n"
        "Current week (all models): 36% used · resets Sep 1 at 7am (Asia/Jerusalem)\n"
    )
    result = usage_lib.parse_usage_output(text, now=now)
    assert result["session_percent"] == 0.0
    assert result["week_percent"] == 36.0
    assert result.get("session_hours_until_reset") is None


def test_calibrate_window_bad_percent(window_state_path):
    result = usage_lib.calibrate_window(150)
    assert result["ok"] is False


def test_calibrate_window_no_transcript(window_state_path, monkeypatch):
    monkeypatch.setattr(usage_lib, "find_known_transcript_path", lambda: None)
    result = usage_lib.calibrate_window(50)
    assert result["ok"] is False


def test_calibrate_window_without_explicit_path_uses_combined_usage(window_state_path, monkeypatch):
    # real_percent is the account's whole 5h-window usage, which includes
    # BOTH squeezer's own daemon-spawned turns and the human's own session —
    # calibrating off only one of those transcripts (as if it were the
    # entire real_percent) understates the true window capacity whenever
    # the other transcript also carries real usage this window.
    state = usage_lib.load_state()
    state["last_known_transcript_path"] = "/fake/human.jsonl"
    state["last_known_human_transcript_path"] = "/fake/human.jsonl"
    state["squeezer_transcript_paths"] = ["/fake/squeezer.jsonl"]
    usage_lib.save_state(state)

    fake_usage = {"/fake/human.jsonl": 100000, "/fake/squeezer.jsonl": 200000}
    monkeypatch.setattr(usage_lib, "sum_usage_since", lambda path, ts: fake_usage[path])

    result = usage_lib.calibrate_window(30)

    assert result["ok"] is True
    assert result["used"] == 300000
    assert result["estimated_window_total"] == int(300000 / 0.30)


def test_calibrate_window_explicit_path_still_uses_just_that_transcript(window_state_path, monkeypatch):
    # An explicit transcript_path (human-relayed cmd_calibrate tied to a
    # specific transcript) is a deliberate override — must NOT be widened
    # to combined usage.
    state = usage_lib.load_state()
    state["squeezer_transcript_paths"] = ["/fake/squeezer.jsonl"]
    usage_lib.save_state(state)

    fake_usage = {"/fake/human.jsonl": 100000, "/fake/squeezer.jsonl": 200000}
    monkeypatch.setattr(usage_lib, "sum_usage_since", lambda path, ts: fake_usage[path])

    result = usage_lib.calibrate_window(30, transcript_path="/fake/human.jsonl")

    assert result["ok"] is True
    assert result["used"] == 100000


def test_cmd_calibrate_still_exits_on_bad_percent(window_state_path):
    with pytest.raises(SystemExit):
        usage_lib.cmd_calibrate(150)


_SELF_CALIBRATE_STDOUT = (
    "Current session: 15% used · resets Aug 23 at 4:09pm (Asia/Jerusalem)\n"
    "Current week (all models): 17% used · resets Aug 25 at 6:59am (Asia/Jerusalem)\n"
)


def test_self_calibrate_success(monkeypatch, window_state_path, weekly_state_path):
    state = usage_lib.load_state()
    state["last_known_transcript_path"] = "/fake/transcript.jsonl"
    state["last_known_human_transcript_path"] = "/fake/transcript.jsonl"
    usage_lib.save_state(state)
    monkeypatch.setattr(usage_lib.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(_SELF_CALIBRATE_STDOUT))
    monkeypatch.setattr(usage_lib, "sum_usage_since", lambda path, ts: 300000)

    result = usage_lib.self_calibrate()

    assert result["ok"] is True
    assert result["session_percent"] == 15.0
    assert result["week_percent"] == 17.0
    assert result["window"]["estimated_window_total"] == int(300000 / 0.15)

    assert usage_lib.load_state()["calibrated"] is True
    assert usage_lib.load_weekly_state()["calibrated"] is True


def test_self_calibrate_subprocess_failure(monkeypatch, window_state_path, weekly_state_path):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=60)
    monkeypatch.setattr(usage_lib.subprocess, "run", _raise)

    result = usage_lib.self_calibrate()
    assert result["ok"] is False
    assert "error" in result


def test_self_calibrate_parse_failure(monkeypatch, window_state_path, weekly_state_path):
    monkeypatch.setattr(usage_lib.subprocess, "run", lambda *a, **k: _FakeCompletedProcess("garbage, no usage lines"))

    result = usage_lib.self_calibrate()
    assert result["ok"] is False
    assert "error" in result


def test_cmd_self_calibrate_exits_on_failure(monkeypatch, capsys):
    monkeypatch.setattr(usage_lib, "self_calibrate", lambda: {"ok": False, "error": "boom"})
    with pytest.raises(SystemExit):
        usage_lib.cmd_self_calibrate()
    assert "boom" in capsys.readouterr().out


def test_cmd_self_calibrate_prints_result_on_success(monkeypatch, capsys):
    monkeypatch.setattr(usage_lib, "self_calibrate", lambda: {"ok": True, "session_percent": 10})
    usage_lib.cmd_self_calibrate()
    assert json.loads(capsys.readouterr().out) == {"ok": True, "session_percent": 10}


def test_find_known_transcript_path_from_state(window_state_path):
    usage_lib.save_state({
        "window_start_ts": usage_lib.now_iso(),
        "estimated_window_total": usage_lib.DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": False,
        "last_known_transcript_path": "/fake/transcript.jsonl",
    })
    assert usage_lib.find_known_transcript_path() == "/fake/transcript.jsonl"


def test_find_known_transcript_path_none_when_unset(window_state_path):
    assert usage_lib.find_known_transcript_path() is None
