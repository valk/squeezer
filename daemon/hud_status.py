#!/usr/bin/env python3
"""Builds squeezer's one-line HUD status — mode/paused, squeezer's own share
of the 5-hour usage window, TODO counts across registered projects, and a
snippet of the most recent worklog entry.
The terminal analogue of the proactive Telegram summaries the daemon already
sends: glanceable state without running the full `/squeezer:status` report.

Reused in three places so there's exactly one place this line is assembled:
- Claude Code's statusLine, in every session (wired up by
  daemon/install_statusline.py during `/squeezer:setup`). It's chained onto
  the global ~/.claude/settings.json statusLine command — appended as an
  extra line after whatever was already configured there (e.g. a plugin
  like claude-hud) rather than replacing it, since statusLine renders one
  row per line of stdout.
- The first line of `/squeezer:status`'s report.
- A header telegram_lib.send_message prepends to every message.

Pure computation (`build_status_line`) takes already-loaded state so it's
unit-testable without mocking the filesystem; the I/O to gather that state is
kept to thin helpers below it.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as _config  # noqa: E402
import usage_lib  # noqa: E402

_OPEN_RE = re.compile(r"^\s*-\s*\[ \]", re.MULTILINE)
_BLOCKED_RE = re.compile(r"^\s*-\s*\[b\]", re.MULTILINE)

# --- squeezer-specific usage bars ("lemony" palette, distinct from
# claude-hud's own Usage bar) ---
_BAR_WIDTH = 5
_ANSI_RESET = "\x1b[0m"
_ANSI_YELLOW = "\x1b[33m"
_ANSI_GREEN = "\x1b[32m"
_ANSI_CYAN = "\x1b[36m"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _mode_fragment(mode: str, paused: bool) -> str:
    fragment = mode
    if paused:
        fragment += "·paused"
    return fragment


def _bar(percent: float, color: str, width: int = _BAR_WIDTH) -> str:
    """A claude-hud-style █░░░░ bar, colorized with an ANSI escape (color
    doesn't count toward width elsewhere, see _visible_len/_truncate_visible
    below). Rounds to the nearest segment (round-half-up) and clamps to
    [0, 100] first so an out-of-range percent can't under/overfill it."""
    clamped = min(max(percent, 0.0), 100.0)
    filled = int(width * clamped / 100 + 0.5)
    glyphs = "█" * filled + "░" * (width - filled)
    return f"{color}{glyphs}{_ANSI_RESET}"


def _squeezer_usage_fragments(
    of_used_percent: float,
    of_window_percent: float,
    of_budget_percent: float,
    budget_of_window_percent: float,
) -> list[str]:
    return [
        f"{_bar(of_used_percent, _ANSI_YELLOW)} {of_used_percent:.0f}% of used",
        f"{_bar(of_window_percent, _ANSI_GREEN)} {of_window_percent:.1f}% of window",
        f"{_bar(of_budget_percent, _ANSI_CYAN)} {of_budget_percent:.0f}% of budget",
        f"budget {budget_of_window_percent:.0f}% of window",
    ]


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _truncate_visible(s: str, width: int) -> str:
    """Truncate to `width` visible characters, preserving ANSI escape codes
    verbatim (they don't count toward width) — plain slicing would either
    cut an escape sequence in half or count its bytes against the budget."""
    out = []
    visible = 0
    i = 0
    while i < len(s) and visible < width:
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        out.append(s[i])
        visible += 1
        i += 1
    return "".join(out).rstrip()


def _todo_fragment(open_count: int, blocked_count: int, project_count: int) -> str:
    if project_count == 0:
        return "no projects registered"
    fragment = f"{open_count} open"
    if blocked_count:
        fragment += f", {blocked_count} blocked"
    return f"{fragment} ({project_count} proj)"


def build_status_line(
    *,
    mode: str,
    paused: bool,
    squeezer_used_percent: float | None,
    squeezer_window_percent: float | None,
    squeezer_budget_percent: float | None,
    squeezer_budget_of_window_percent: float | None,
    open_count: int,
    blocked_count: int,
    project_count: int,
    last_insight: str | None,
    width: int = 0,
) -> str:
    """Pure assembly of the ranked fragments (state -> squeezer's usage bars
    -> TODOs -> insight), truncated to `width` columns if given (0/falsy =
    no limit). The four squeezer_* percents come as a set — all None omits
    the usage bars entirely (uncalibrated window, same fail-open convention
    as the rest of usage_lib) rather than showing them as zero:
    - squeezer_used_percent: squeezer's share of tokens used so far this
      window (human + squeezer combined) — e.g. 7% of a 14%-used window is
      50% "of used".
    - squeezer_window_percent: squeezer's usage as a share of the full
      window capacity (independent of how much of the window is used so
      far) — the 7% itself in the example above.
    - squeezer_budget_percent: squeezer's usage as a share of its own
      *allowed* budget this window (estimated_window_total minus the
      configured reserve — 0 reserve, i.e. 100%, during no_reserve_hours) —
      how close squeezer is to its own cap right now.
    - squeezer_budget_of_window_percent: how much of the full window
      squeezer's allowed budget itself represents (100% minus the reserve
      percent) — varies with time of day via no_reserve_hours."""
    parts = [_mode_fragment(mode, paused)]
    if all(
        p is not None
        for p in (
            squeezer_used_percent, squeezer_window_percent,
            squeezer_budget_percent, squeezer_budget_of_window_percent,
        )
    ):
        parts.extend(_squeezer_usage_fragments(
            squeezer_used_percent, squeezer_window_percent,
            squeezer_budget_percent, squeezer_budget_of_window_percent,
        ))
    parts.append(_todo_fragment(open_count, blocked_count, project_count))
    if last_insight:
        parts.append(last_insight)
    line = "\U0001f34b " + " · ".join(parts)
    if width and _visible_len(line) > width:
        line = _truncate_visible(line, max(0, width - 1)) + _ANSI_RESET + "…"
    return line


def _squeezer_usage_percents() -> tuple[float, float, float, float] | None:
    """(of_used, of_window, of_budget, budget_of_window) — see
    build_status_line's docstring for what each means — or None if the
    window hasn't been calibrated yet (same fail-open convention as the
    rest of usage_lib).

    total_used comes from usage_lib.total_used_since, NOT
    find_known_transcript_path()/last_known_transcript_path: that pointer is
    overwritten by whichever session's PreToolUse hook fires last, including
    squeezer's own daemon-spawned turns, so right after squeezer runs a turn
    it stops meaning "total usage" and starts meaning "squeezer's own latest
    turn" — collapsing of_used to ~100% (or, once squeezer_used sums
    multiple daemon turns against that single narrow transcript, past
    100%)."""
    state = usage_lib.load_state()
    if not state.get("calibrated"):
        return None
    total_used = usage_lib.total_used_since(state)
    squeezer_used = usage_lib.sum_squeezer_usage_since(state["window_start_ts"])
    estimated_total = state["estimated_window_total"]
    allowed_max = estimated_total * (1 - usage_lib.load_reserve_percent() / 100)

    of_used = 100 * squeezer_used / total_used if total_used else 0.0
    of_window = 100 * squeezer_used / estimated_total
    of_budget = 100 * squeezer_used / allowed_max if allowed_max else 0.0
    budget_of_window = 100 * allowed_max / estimated_total
    return of_used, of_window, of_budget, budget_of_window


def _todo_counts() -> tuple[int, int, int]:
    """(open_count, blocked_count, project_count) summed across every
    todos/<project>/TODO.md — deliberately excludes the top-level
    todos/TODO.md cross-project view, which just links to these same items."""
    open_count = blocked_count = project_count = 0
    todos_dir = _config.todos_dir()
    for project_dir in sorted(todos_dir.iterdir()):
        todo_file = project_dir / "TODO.md"
        if not project_dir.is_dir() or not todo_file.exists():
            continue
        text = todo_file.read_text()
        project_count += 1
        open_count += len(_OPEN_RE.findall(text))
        blocked_count += len(_BLOCKED_RE.findall(text))
    return open_count, blocked_count, project_count


def _last_insight(max_len: int = 70) -> str | None:
    """Last top-level bullet ('- ' at column 0 — see CLAUDE.md.template's
    "flush to state/worklog.md before ending your turn": entries accumulate
    under the same '## <date>' heading across a day, appended in
    chronological order) under the most recent date heading in
    state/worklog.md. Indented sub-bullets and wrapped continuation lines
    are deliberately skipped so this surfaces the latest entry, not a
    fragment of it."""
    worklog = _config.state_dir() / "worklog.md"
    if not worklog.exists():
        return None
    sections = re.split(r"(?m)^## ", worklog.read_text())
    if len(sections) < 2:
        return None
    top_level_bullets = [
        line[2:].strip() for line in sections[-1].splitlines()[1:] if line.startswith("- ")
    ]
    if not top_level_bullets:
        return None
    line = top_level_bullets[-1]
    return line if len(line) <= max_len else line[: max_len - 1].rstrip() + "…"


def _terminal_width() -> int:
    try:
        return int(os.environ.get("COLUMNS", ""))
    except ValueError:
        return 0


def current_status_line(width: int = None) -> str:
    """Gathers live state from SQUEEZER_HOME and assembles the line — the
    one entry point every caller (statusLine script, /squeezer:status,
    telegram_lib) actually uses."""
    cfg = _config.load_config()
    open_count, blocked_count, project_count = _todo_counts()
    usage_percents = _squeezer_usage_percents()
    (
        squeezer_used_percent, squeezer_window_percent,
        squeezer_budget_percent, squeezer_budget_of_window_percent,
    ) = usage_percents if usage_percents else (None, None, None, None)
    return build_status_line(
        mode=cfg.get("mode", "auto"),
        paused=(_config.state_dir() / "paused").exists(),
        squeezer_used_percent=squeezer_used_percent,
        squeezer_window_percent=squeezer_window_percent,
        squeezer_budget_percent=squeezer_budget_percent,
        squeezer_budget_of_window_percent=squeezer_budget_of_window_percent,
        open_count=open_count,
        blocked_count=blocked_count,
        project_count=project_count,
        last_insight=_last_insight(),
        width=_terminal_width() if width is None else width,
    )


if __name__ == "__main__":
    try:
        print(current_status_line())
    except Exception as e:  # never break a statusLine render / message send
        print(f"\U0001f34b squeezer: status unavailable ({e})")
