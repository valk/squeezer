"""Tests for daemon/hud_status.py — the one-line HUD summary (mode/budget,
TODO counts, latest worklog insight) shared by SQUEEZER_HOME's statusLine,
`/squeezer:status`, and the Telegram message header."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("hud_status", SQUEEZER_DIR / "daemon" / "hud_status.py")
hud_status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hud_status)


@pytest.fixture(autouse=True)
def _isolate_usage_lib_state(tmp_path, monkeypatch):
    """usage_lib.STATE_PATH is a module-level constant resolved once at
    import time (see test_usage_lib.py's window_state_path fixture, which
    works around the same thing) — without this, current_status_line()'s
    call into usage_lib would read/write the real SQUEEZER_HOME's
    state/window_budget.json instead of this test's scratch one."""
    monkeypatch.setattr(hud_status.usage_lib, "STATE_PATH", tmp_path / "window_budget.json")


# --- build_status_line: pure assembly, no I/O ---

def test_build_status_line_includes_all_ranked_fragments():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_used_percent=50.0, squeezer_window_percent=2.5,
        squeezer_budget_percent=30.0, squeezer_budget_of_window_percent=80.0,
        open_count=5, blocked_count=1, project_count=2,
        last_insight="fixed the flaky test",
    )
    assert "auto" in line
    assert "50% of used" in line
    assert "2.5% of window" in line
    assert "30% of budget" in line
    assert "budget 80% of window" in line
    assert "█" in line and "░" in line
    assert "5 open, 1 blocked (2 proj)" in line
    assert "fixed the flaky test" in line


def test_build_status_line_shows_paused():
    line = hud_status.build_status_line(
        mode="auto", paused=True,
        squeezer_used_percent=None, squeezer_window_percent=None,
        squeezer_budget_percent=None, squeezer_budget_of_window_percent=None,
        open_count=0, blocked_count=0, project_count=0, last_insight=None,
    )
    assert "auto·paused" in line


def test_build_status_line_omits_usage_bars_when_uncalibrated():
    line = hud_status.build_status_line(
        mode="human_in_loop", paused=False,
        squeezer_used_percent=None, squeezer_window_percent=None,
        squeezer_budget_percent=None, squeezer_budget_of_window_percent=None,
        open_count=1, blocked_count=0, project_count=1, last_insight=None,
    )
    assert "of used" not in line
    assert "of window" not in line
    assert "of budget" not in line


def test_build_status_line_no_projects_registered():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_used_percent=None, squeezer_window_percent=None,
        squeezer_budget_percent=None, squeezer_budget_of_window_percent=None,
        open_count=0, blocked_count=0, project_count=0, last_insight=None,
    )
    assert "no projects registered" in line


def test_build_status_line_omits_blocked_when_zero():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_used_percent=None, squeezer_window_percent=None,
        squeezer_budget_percent=None, squeezer_budget_of_window_percent=None,
        open_count=3, blocked_count=0, project_count=1, last_insight=None,
    )
    assert "3 open (1 proj)" in line
    assert "blocked" not in line


def test_build_status_line_truncates_to_width():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_used_percent=87.0, squeezer_window_percent=13.0,
        squeezer_budget_percent=95.0, squeezer_budget_of_window_percent=80.0,
        open_count=5, blocked_count=1, project_count=2,
        last_insight="a very long worklog snippet that should get cut off",
        width=30,
    )
    # <= not == : a trailing space right at the cut point gets rstripped
    # (no dangling space before the ellipsis), which can land 1 char short.
    assert hud_status._visible_len(line) <= 30
    assert line.endswith("…")


# --- _bar: bar rendering ---

def test_bar_zero_percent():
    assert hud_status._bar(0, hud_status._ANSI_YELLOW) == (
        f"{hud_status._ANSI_YELLOW}░░░░░{hud_status._ANSI_RESET}"
    )


def test_bar_full_percent():
    assert hud_status._bar(100, hud_status._ANSI_YELLOW) == (
        f"{hud_status._ANSI_YELLOW}█████{hud_status._ANSI_RESET}"
    )


def test_bar_rounds_to_nearest_segment():
    assert hud_status._bar(50, hud_status._ANSI_YELLOW, width=5) == (
        f"{hud_status._ANSI_YELLOW}███░░{hud_status._ANSI_RESET}"
    )


def test_bar_clamps_out_of_range_percent():
    assert hud_status._bar(150, hud_status._ANSI_YELLOW) == (
        f"{hud_status._ANSI_YELLOW}█████{hud_status._ANSI_RESET}"
    )
    assert hud_status._bar(-10, hud_status._ANSI_YELLOW) == (
        f"{hud_status._ANSI_YELLOW}░░░░░{hud_status._ANSI_RESET}"
    )


# --- current_status_line: real I/O against a scratch SQUEEZER_HOME ---

def _write_config(tmp_path, **overrides):
    cfg = {"mode": "auto", **overrides}
    (tmp_path / "config.json").write_text(json.dumps(cfg))


def test_current_status_line_counts_todos_across_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    (tmp_path / "todos" / "acme").mkdir(parents=True)
    (tmp_path / "todos" / "acme" / "TODO.md").write_text(
        "- [ ] open item one\n- [b] blocked item\n- [x] done item\n"
    )
    (tmp_path / "todos" / "beta").mkdir(parents=True)
    (tmp_path / "todos" / "beta" / "TODO.md").write_text("- [ ] another open item\n")
    # The cross-project summary file itself must not be double-counted.
    (tmp_path / "todos" / "TODO.md").write_text("- [ ] [acme] open item one\n")

    line = hud_status.current_status_line()
    assert "2 open, 1 blocked (2 proj)" in line


def test_current_status_line_reports_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "paused").write_text("")

    assert "paused" in hud_status.current_status_line()


def _write_calibrated_state(tmp_path, total_used, squeezer_used, estimated_window_total=10000):
    """Two separate scratch transcripts: one standing in for the human's own
    session, one for squeezer's own tracked daemon session (squeezer_used
    tokens) — modeling the common case where the two differ. hud_status now
    derives total_used as human + squeezer (see total_used_since), so the
    human transcript here is seeded with the remainder (total_used minus
    squeezer_used) rather than total_used itself, keeping this helper's
    total_used param meaning "the window grand total" for callers."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(minutes=10)).isoformat()

    def _entry(tokens):
        return json.dumps({"timestamp": now.isoformat(), "message": {"usage": {"input_tokens": tokens}}})

    human_transcript = tmp_path / "human.jsonl"
    squeezer_transcript = tmp_path / "squeezer.jsonl"
    human_transcript.write_text(_entry(total_used - squeezer_used) + "\n")
    squeezer_transcript.write_text(_entry(squeezer_used) + "\n")

    hud_status.usage_lib.save_state({
        "window_start_ts": since,
        "estimated_window_total": estimated_window_total,
        "past_window_totals": [],
        "calibrated": True,
        "last_known_human_transcript_path": str(human_transcript),
        "squeezer_transcript_paths": [str(squeezer_transcript)],
    })


def test_current_status_line_shows_squeezer_usage_bars(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)  # reserve_percent defaults to 20 -> budget is 80% of the window
    _write_calibrated_state(tmp_path, total_used=1000, squeezer_used=400, estimated_window_total=10000)

    line = hud_status.current_status_line()
    assert "40% of used" in line
    assert "4.0% of window" in line
    # budget = 10000 * 0.8 = 8000; 400/8000 = 5%
    assert "5% of budget" in line
    assert "budget 80% of window" in line


def test_current_status_line_shows_zero_percent_bars_when_squeezer_has_not_run_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    _write_calibrated_state(tmp_path, total_used=1000, squeezer_used=0, estimated_window_total=10000)

    line = hud_status.current_status_line()
    assert "0% of used" in line
    assert "0.0% of window" in line
    assert "0% of budget" in line
    assert "budget 80% of window" in line


def test_current_status_line_of_budget_reflects_no_reserve_hours(tmp_path, monkeypatch):
    """During the configured no_reserve_hours window, load_reserve_percent()
    returns 0 — squeezer's whole budget IS the window, e.g. at night."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path, no_reserve_hours={"start": "00:00", "end": "23:59"})
    _write_calibrated_state(tmp_path, total_used=1000, squeezer_used=400, estimated_window_total=10000)

    line = hud_status.current_status_line()
    # budget = 10000 * 1.0 = 10000; 400/10000 = 4%
    assert "4% of budget" in line
    assert "budget 100% of window" in line


def test_of_used_stays_bounded_across_multiple_squeezer_turns(tmp_path, monkeypatch):
    """Regression for the bug where of_used jumped 0% -> 100% right after
    squeezer's first daemon-spawned turn, then past 100% (e.g. 270%) as
    squeezer ran more turns — caused by comparing squeezer_used (summed
    across every squeezer transcript) against a "total" that was really
    just whichever single transcript a stale last-known pointer landed on.
    Three squeezer turns of 90 tokens each vs. one human turn of 100."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)

    now = datetime.now(timezone.utc)
    since = (now - timedelta(minutes=10)).isoformat()

    def _entry(tokens):
        return json.dumps({"timestamp": now.isoformat(), "message": {"usage": {"input_tokens": tokens}}})

    human = tmp_path / "human.jsonl"
    squeezer_turns = [tmp_path / f"squeezer{i}.jsonl" for i in range(3)]
    human.write_text(_entry(100) + "\n")
    for path in squeezer_turns:
        path.write_text(_entry(90) + "\n")

    hud_status.usage_lib.save_state({
        "window_start_ts": since,
        "estimated_window_total": 10000,
        "past_window_totals": [],
        "calibrated": True,
        # the generic pointer landing on just the latest squeezer turn is
        # exactly what previously broke this.
        "last_known_transcript_path": str(squeezer_turns[-1]),
        "last_known_human_transcript_path": str(human),
        "squeezer_transcript_paths": [str(p) for p in squeezer_turns],
    })

    line = hud_status.current_status_line()
    # squeezer_used=270, total=100+270=370 -> ~73%, never >= 100%.
    assert "73% of used" in line
    assert "100% of used" not in line
    # budget = 10000 * 0.8 = 8000; 270/8000 ~= 3.4%
    assert "3% of budget" in line


def test_current_status_line_omits_usage_bars_when_uncalibrated(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    # default state from load_state() has calibrated=False

    line = hud_status.current_status_line()
    assert "of used" not in line
    assert "of window" not in line
    assert "of budget" not in line


def test_current_status_line_includes_latest_worklog_insight(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "worklog.md").write_text(
        "# Worklog\n\n## 2026-08-21\n- old entry, ignore this one\n\n"
        "## 2026-08-27\n- an earlier entry from the same day\n"
        "- shipped the HUD status line feature\n"
    )

    assert "shipped the HUD status line feature" in hud_status.current_status_line()


def test_last_insight_returns_none_without_worklog(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True)
    assert hud_status._last_insight() is None


def test_last_insight_picks_the_last_bullet_of_the_day_not_the_first(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "worklog.md").write_text(
        "## 2026-08-27\n"
        "- earliest entry of the day, appended first\n"
        "  - a sub-bullet elaborating on it, should be skipped\n"
        "- a later entry appended after the first one finished\n"
    )
    assert hud_status._last_insight() == "a later entry appended after the first one finished"


def test_last_insight_truncates_long_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True)
    long_line = "x" * 100
    (tmp_path / "state" / "worklog.md").write_text(f"## 2026-08-27\n- {long_line}\n")
    insight = hud_status._last_insight(max_len=20)
    assert len(insight) == 20
    assert insight.endswith("…")
