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


def _bar_zone_widths(bar: str) -> list[int]:
    """Splits a _context_bar() string on _ANSI_RESET (each of the four
    zones is terminated by one) and measures each zone's plain glyph count
    — lets tests check per-zone width without hardcoding color escapes."""
    chunks = bar.split(hud_status._ANSI_RESET)[:-1]
    return [len(hud_status._ANSI_RE.sub("", c)) for c in chunks]


# --- build_status_line: pure assembly, no I/O ---

def test_build_status_line_includes_all_ranked_fragments():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_window_percent=10.0, human_window_percent=6.0,
        squeezer_budget_percent=30.0, squeezer_budget_of_window_percent=80.0,
        open_count=5, blocked_count=1, project_count=2,
        last_insight="fixed the flaky test",
    )
    assert "auto" in line
    assert "squeezed: 30%" in line
    assert "user: 6%" in line
    assert "max: 80% of the 5h window" in line
    assert "total: 16%" in line
    assert "█" in line and "░" in line
    assert "5 open, 1 blocked (2 projects)" in line
    assert "fixed the flaky test" in line


def test_build_status_line_shows_paused():
    line = hud_status.build_status_line(
        mode="auto", paused=True,
        squeezer_window_percent=None, human_window_percent=None,
        squeezer_budget_percent=None, squeezer_budget_of_window_percent=None,
        open_count=0, blocked_count=0, project_count=0, last_insight=None,
    )
    assert "auto·paused" in line


def test_build_status_line_shows_zero_usage_bar_when_uncalibrated():
    """Before the window's calibrated (e.g. right after install), the four
    usage percents come in as None — the bar renders with zeros instead of
    disappearing, so the HUD row is visible immediately rather than only
    once real usage data exists."""
    line = hud_status.build_status_line(
        mode="human_in_loop", paused=False,
        squeezer_window_percent=None, human_window_percent=None,
        squeezer_budget_percent=None, squeezer_budget_of_window_percent=None,
        open_count=1, blocked_count=0, project_count=1, last_insight=None,
    )
    assert "squeezed: 0%" in line
    assert "user: 0%" in line
    assert "max: 0% of the 5h window" in line
    assert "total: 0%" in line


def test_build_status_line_no_projects_registered():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_window_percent=None, human_window_percent=None,
        squeezer_budget_percent=None, squeezer_budget_of_window_percent=None,
        open_count=0, blocked_count=0, project_count=0, last_insight=None,
    )
    assert "no projects registered" in line


def test_build_status_line_omits_blocked_when_zero():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_window_percent=None, human_window_percent=None,
        squeezer_budget_percent=None, squeezer_budget_of_window_percent=None,
        open_count=3, blocked_count=0, project_count=1, last_insight=None,
    )
    assert "3 open (1 project)" in line
    assert "blocked" not in line


def test_build_status_line_truncates_to_width():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_window_percent=70.0, human_window_percent=5.0,
        squeezer_budget_percent=95.0, squeezer_budget_of_window_percent=80.0,
        open_count=5, blocked_count=1, project_count=2,
        last_insight="a very long worklog snippet that should get cut off",
        width=30,
    )
    # <= not == : a trailing space right at the cut point gets rstripped
    # (no dangling space before the ellipsis), which can land 1 char short.
    assert hud_status._visible_len(line) <= 30
    assert line.endswith("…")


# --- _allocate_chars: largest-remainder rounding for bar zone widths ---

def test_allocate_chars_sums_to_exact_width():
    assert sum(hud_status._allocate_chars([10, 70, 6, 14], 20)) == 20


def test_allocate_chars_distributes_by_largest_remainder():
    assert hud_status._allocate_chars([10, 70, 6, 14], 20) == [2, 14, 1, 3]


def test_allocate_chars_handles_zero_shares():
    assert hud_status._allocate_chars([0, 100, 0, 0], 20) == [0, 20, 0, 0]


# --- _context_bar: the four-zone squeeze/headroom/user/tail bar ---

def test_context_bar_renders_at_exact_width():
    bar = hud_status._context_bar(10.0, 6.0, 30.0, 80.0, width=20)
    assert hud_status._visible_len(bar) == 20


def test_context_bar_color_false_has_no_ansi_escapes():
    """Telegram renders plain text and mangles raw ANSI escapes into
    literal garbage rather than interpreting them (the bug this guards
    against), so color=False must emit none."""
    bar = hud_status._context_bar(10.0, 6.0, 30.0, 80.0, width=20, color=False)
    assert "\x1b" not in bar
    assert hud_status._visible_len(bar) == 20


def test_context_bar_color_false_still_distinguishes_all_four_zones():
    """Without color, the four zones must stay visually distinct by glyph
    alone, or the bar collapses into an ambiguous wall of blocks."""
    bar = hud_status._context_bar(10.0, 6.0, 30.0, 80.0, width=20, color=False)
    assert len(set(bar)) == 4


def test_build_status_line_color_false_has_no_ansi_escapes():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_window_percent=10.0, human_window_percent=6.0,
        squeezer_budget_percent=30.0, squeezer_budget_of_window_percent=80.0,
        open_count=5, blocked_count=1, project_count=2,
        last_insight="fixed the flaky test",
        color=False,
    )
    assert "\x1b" not in line
    assert "squeezed: 30%" in line


def test_build_status_line_color_false_truncates_without_trailing_escape():
    line = hud_status.build_status_line(
        mode="auto", paused=False,
        squeezer_window_percent=70.0, human_window_percent=5.0,
        squeezer_budget_percent=95.0, squeezer_budget_of_window_percent=80.0,
        open_count=5, blocked_count=1, project_count=2,
        last_insight="a very long worklog snippet that should get cut off",
        width=30, color=False,
    )
    assert "\x1b" not in line
    assert len(line) <= 30
    assert line.endswith("…")


def test_context_bar_zone_widths_match_percentages():
    bar = hud_status._context_bar(10.0, 6.0, 30.0, 80.0, width=20)
    assert _bar_zone_widths(bar) == [2, 14, 1, 3]


def test_context_bar_squeezer_zone_clamped_when_over_budget():
    """If squeezer's raw usage exceeds its own allowed max (of_budget_percent
    > 100%, see _squeezer_usage_percents), the squeeze zone fills solid with
    zero headroom rather than overflowing into the human's zone."""
    bar = hud_status._context_bar(90.0, 5.0, 150.0, 80.0, width=20)
    a, b, c, d = _bar_zone_widths(bar)
    assert b == 0
    assert a == 16  # 80% allowed max * 20 chars
    assert a + b + c + d == 20


def test_context_bar_uses_squeeze_color_for_solid_zone():
    bar = hud_status._context_bar(10.0, 6.0, 30.0, 80.0, width=20)
    assert bar.startswith(hud_status._squeeze_color(30.0))


# --- _squeeze_color: yellow -> green gradient toward the allowed max ---

def test_squeeze_color_at_zero_is_pure_yellow():
    assert hud_status._squeeze_color(0) == "\x1b[38;5;226m"


def test_squeeze_color_at_full_is_pure_green():
    assert hud_status._squeeze_color(100) == "\x1b[38;5;46m"


def test_squeeze_color_midpoint_is_between_yellow_and_green():
    mid = hud_status._squeeze_color(50)
    assert mid not in ("\x1b[38;5;226m", "\x1b[38;5;46m")


def test_squeeze_color_clamps_out_of_range_percent():
    assert hud_status._squeeze_color(150) == "\x1b[38;5;46m"
    assert hud_status._squeeze_color(-10) == "\x1b[38;5;226m"


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
    assert "2 open, 1 blocked (2 projects)" in line


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


def test_current_status_line_shows_squeezer_usage_bar(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)  # reserve_percent defaults to 20
    _write_calibrated_state(tmp_path, total_used=1000, squeezer_used=400, estimated_window_total=10000)

    line = hud_status.current_status_line()
    # squeezer_window=4%, human_window=6%; allowed max=(100-20)-6=74%=7400
    # tokens; 400/7400 ~= 5.4%; total=4+6=10%
    assert "squeezed: 5%" in line
    assert "user: 6%" in line
    assert "max: 74% of the 5h window" in line
    assert "total: 10%" in line


def test_current_status_line_shows_zero_percent_bar_when_squeezer_has_not_run_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    _write_calibrated_state(tmp_path, total_used=1000, squeezer_used=0, estimated_window_total=10000)

    line = hud_status.current_status_line()
    # human_window=10%; allowed max = (100-20)-10 = 70% of the 5h window
    assert "squeezed: 0%" in line
    assert "max: 70% of the 5h window" in line


def test_current_status_line_of_budget_reflects_no_reserve_hours(tmp_path, monkeypatch):
    """During the configured no_reserve_hours window, load_reserve_percent()
    returns 0 — squeezer's allowed maximum is the 5h window minus only
    however much the human has directly used, e.g. at night."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path, no_reserve_hours={"start": "00:00", "end": "23:59"})
    _write_calibrated_state(tmp_path, total_used=1000, squeezer_used=400, estimated_window_total=10000)

    line = hud_status.current_status_line()
    # human_window=6%; allowed max = 100-6 = 94% = 9400 tokens; 400/9400 ~= 4.3%
    assert "squeezed: 4%" in line
    assert "max: 94% of the 5h window" in line


def test_current_status_line_allowed_max_never_goes_below_zero(tmp_path, monkeypatch):
    """If the human's own direct usage alone already exceeds (100% - reserve%)
    of the 5h window, the allowed maximum is clamped to 0 rather than going
    negative — squeezer is already fully blocked in that case (per
    usage_lib.budget_ok), not "over-squeezed"."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)  # reserve_percent defaults to 20 -> threshold 80%
    # human alone used 9000 (90% of the 5h window), squeezer used none yet.
    _write_calibrated_state(tmp_path, total_used=9000, squeezer_used=0, estimated_window_total=10000)

    line = hud_status.current_status_line()
    assert "squeezed: 0%" in line
    assert "max: 0% of the 5h window" in line
    assert "user: 90%" in line
    assert "total: 90%" in line


def test_budget_percent_correctly_sums_multiple_squeezer_turns(tmp_path, monkeypatch):
    """Regression for the bug where a prior version of this metric compared
    squeezer_used (summed across every squeezer transcript) against a
    "total" that was really just whichever single transcript a stale
    last-known pointer landed on. Three squeezer turns of 90 tokens each
    vs. one human turn of 100 — squeezer_used must come out as 270, not
    just the latest turn's 90."""
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
    # squeezer_used=270 (2.7% of window), human_used=100 (1%); allowed max =
    # 80-1 = 79% = 7900 tokens; 270/7900 ~= 3.4%; total=2.7+1=3.7 -> "4%"
    assert "squeezed: 3%" in line
    assert "max: 79% of the 5h window" in line
    assert "total: 4%" in line


def test_current_status_line_rolls_an_overdue_window_before_computing_percents(tmp_path, monkeypatch):
    """Regression: window rollover previously only ever happened on the
    daemon's own 20-minute self_calibrate_loop timer, so a statusLine render
    against a window left stale by a dead/missing daemon kept reporting
    squeezer usage from an already-expired window (e.g. "squeezed: 11%"
    despite squeezer not having run any turn since). A render must self-heal
    an overdue window before computing anything against it, same as
    usage_lib.cmd_check now does."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    stale_start = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    old_squeezer_transcript = tmp_path / "old-window-squeezer-turn.jsonl"
    old_squeezer_transcript.write_text(json.dumps({
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=5, minutes=30)).isoformat(),
        "message": {"usage": {"input_tokens": 4000}},
    }) + "\n")
    hud_status.usage_lib.save_state({
        "window_start_ts": stale_start,
        "estimated_window_total": 10000,
        "past_window_totals": [],
        "calibrated": True,
        "squeezer_transcript_paths": [str(old_squeezer_transcript)],
    })

    line = hud_status.current_status_line()

    assert "squeezed: 0%" in line
    assert hud_status.usage_lib.load_state()["window_start_ts"] != stale_start


def test_current_status_line_shows_zero_usage_bar_when_uncalibrated(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    # default state from load_state() has calibrated=False

    line = hud_status.current_status_line()
    assert "squeezed: 0%" in line


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


# --- _real_five_hour_percent_from_stdin: Claude Code's real rate_limits ---

class _FakeTTY:
    def isatty(self):
        return True


def test_real_five_hour_percent_from_stdin_skips_interactive_tty(monkeypatch):
    monkeypatch.setattr(hud_status.sys, "stdin", _FakeTTY())
    assert hud_status._real_five_hour_percent_from_stdin() is None


def test_real_five_hour_percent_from_stdin_parses_rate_limits(monkeypatch):
    import io
    payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 36}}})
    monkeypatch.setattr(hud_status.sys, "stdin", io.StringIO(payload))
    assert hud_status._real_five_hour_percent_from_stdin() == 36.0


def test_real_five_hour_percent_from_stdin_none_on_empty_stdin(monkeypatch):
    import io
    monkeypatch.setattr(hud_status.sys, "stdin", io.StringIO(""))
    assert hud_status._real_five_hour_percent_from_stdin() is None


def test_real_five_hour_percent_from_stdin_none_on_malformed_json(monkeypatch):
    import io
    monkeypatch.setattr(hud_status.sys, "stdin", io.StringIO("not json"))
    assert hud_status._real_five_hour_percent_from_stdin() is None


def test_real_five_hour_percent_from_stdin_none_when_rate_limits_missing(monkeypatch):
    import io
    monkeypatch.setattr(hud_status.sys, "stdin", io.StringIO(json.dumps({"model": {"id": "x"}})))
    assert hud_status._real_five_hour_percent_from_stdin() is None


def test_real_five_hour_percent_from_stdin_none_on_non_numeric_percentage(monkeypatch):
    import io
    payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": "36"}}})
    monkeypatch.setattr(hud_status.sys, "stdin", io.StringIO(payload))
    assert hud_status._real_five_hour_percent_from_stdin() is None


# --- current_status_line reconciled against a real rate_limits percent ---

def test_current_status_line_reconciles_total_to_real_five_hour_percent(tmp_path, monkeypatch):
    """total: N% must land on exactly the real percent claude-hud shows
    (via Claude Code's own rate_limits), not squeezer's own drifting
    self-calibrated estimate — even when the opportunistic recalibration
    itself fails open (no last_known_transcript_path set here)."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    _write_calibrated_state(tmp_path, total_used=1000, squeezer_used=400, estimated_window_total=10000)

    line = hud_status.current_status_line(real_five_hour_percent=36.0)
    # squeezer's fraction of activity this window: 400/1000 = 0.4
    # squeezer_window = 36 * 0.4 = 14.4; human_window = 21.6 -> "22%"
    # allowed max = (100-20) - 21.6 = 58.4% = 5840 (of the *old*, uncalibrated
    # estimated_total=10000, since no last_known_transcript_path is set here
    # for calibrate_window to succeed against); squeezed = 400/5840 ~= 7%
    assert "total: 36%" in line
    assert "squeezed: 7%" in line
    assert "user: 22%" in line
    assert "max: 58% of the 5h window" in line


def test_current_status_line_falls_back_to_estimate_without_real_percent(tmp_path, monkeypatch):
    """Same scratch state as the reconciliation test above, but without a
    real percent — must reproduce squeezer's own self-estimated numbers
    unchanged (today's behavior, still exercised by the default None)."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    _write_calibrated_state(tmp_path, total_used=1000, squeezer_used=400, estimated_window_total=10000)

    line = hud_status.current_status_line()
    assert "total: 10%" in line
    assert "user: 6%" in line


def test_current_status_line_recalibrates_estimated_window_total_from_real_percent(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("COLUMNS", raising=False)
    _write_config(tmp_path)
    _write_calibrated_state(tmp_path, total_used=1000, squeezer_used=400, estimated_window_total=10000)
    state = hud_status.usage_lib.load_state()
    state["last_known_transcript_path"] = state["squeezer_transcript_paths"][0]
    hud_status.usage_lib.save_state(state)

    hud_status.current_status_line(real_five_hour_percent=40.0)

    # squeezer's own transcript has 400 tokens = 40% real -> estimated total = 400/0.40 = 1000
    assert hud_status.usage_lib.load_state()["estimated_window_total"] == 1000


def test_last_insight_truncates_long_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True)
    long_line = "x" * 100
    (tmp_path / "state" / "worklog.md").write_text(f"## 2026-08-27\n- {long_line}\n")
    insight = hud_status._last_insight(max_len=20)
    assert len(insight) == 20
    assert insight.endswith("…")
