#!/usr/bin/env python3
"""Builds squeezer's one-line HUD status — mode/budget/paused, TODO counts
across registered projects, and a snippet of the most recent worklog entry.
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


def _mode_fragment(mode: str, paused: bool, window_percent: float | None) -> str:
    fragment = mode
    if paused:
        fragment += "·paused"
    if window_percent is not None:
        fragment += f"·{window_percent:.0f}% window"
    return fragment


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
    window_percent: float | None,
    open_count: int,
    blocked_count: int,
    project_count: int,
    last_insight: str | None,
    width: int = 0,
) -> str:
    """Pure assembly of the three ranked fragments (state -> TODOs ->
    insight), truncated to `width` columns if given (0/falsy = no limit)."""
    parts = [
        _mode_fragment(mode, paused, window_percent),
        _todo_fragment(open_count, blocked_count, project_count),
    ]
    if last_insight:
        parts.append(last_insight)
    line = "\U0001f34b " + " · ".join(parts)
    if width and len(line) > width:
        line = line[: max(0, width - 1)].rstrip() + "…"
    return line


def _window_percent() -> float | None:
    state = usage_lib.load_state()
    if not state.get("calibrated"):
        return None
    transcript_path = usage_lib.find_known_transcript_path()
    if not transcript_path:
        return None
    used = usage_lib.sum_usage_since(transcript_path, state["window_start_ts"])
    return 100 * used / state["estimated_window_total"]


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
    return build_status_line(
        mode=cfg.get("mode", "auto"),
        paused=(_config.state_dir() / "paused").exists(),
        window_percent=_window_percent(),
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
