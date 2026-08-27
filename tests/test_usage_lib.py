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


def test_calibrate_window_bad_percent(window_state_path):
    result = usage_lib.calibrate_window(150)
    assert result["ok"] is False


def test_calibrate_window_no_transcript(window_state_path, monkeypatch):
    monkeypatch.setattr(usage_lib, "find_known_transcript_path", lambda: None)
    result = usage_lib.calibrate_window(50)
    assert result["ok"] is False


def test_cmd_calibrate_still_exits_on_bad_percent(window_state_path):
    with pytest.raises(SystemExit):
        usage_lib.cmd_calibrate(150)


_SELF_CALIBRATE_STDOUT = (
    "Current session: 15% used · resets Aug 23 at 4:09pm (Asia/Jerusalem)\n"
    "Current week (all models): 17% used · resets Aug 25 at 6:59am (Asia/Jerusalem)\n"
)


def test_self_calibrate_success(monkeypatch, window_state_path, weekly_state_path):
    monkeypatch.setattr(usage_lib.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(_SELF_CALIBRATE_STDOUT))
    monkeypatch.setattr(usage_lib, "find_known_transcript_path", lambda: "/fake/transcript.jsonl")
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
