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
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as _config  # noqa: E402
import usage_lib  # noqa: E402

_OPEN_RE = re.compile(r"^\s*-\s*\[ \]", re.MULTILINE)
_BLOCKED_RE = re.compile(r"^\s*-\s*\[b\]", re.MULTILINE)

# --- squeezer-specific usage bar ("lemony" palette, distinct from
# claude-hud's own Usage bar) — a single bar spanning 0-100% of the 5-hour
# rolling token window (labeled "the 5h window" to stay plain and avoid
# colliding with claude-hud's own "Usage"/"Context" bars — claude-hud's
# "Context" is the unrelated current-session context-window fill), made of
# four zones left to right:
#   1. squeezer's own usage (solid, yellow -> green as it nears its cap)
#   2. squeezer's remaining headroom within its allowed max (dim yellow
#      dots) — this zone's width shrinks as the human's direct usage eats
#      into the shared reserve, per _squeezer_usage_percents
#   3. the human's own direct usage (solid blue)
#   4. whatever's left untouched by either (dim neutral dots)
_CONTEXT_BAR_WIDTH = 20
_ANSI_RESET = "\x1b[0m"
_ANSI_DIM_YELLOW = "\x1b[2;33m"
_ANSI_BRIGHT_BLUE = "\x1b[94m"
_ANSI_DIM = "\x1b[2m"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _squeeze_color(percent: float) -> str:
    """Interpolates yellow -> green as squeezer approaches its allowed
    maximum, walking the xterm 256-color cube's r=5..0,g=5,b=0 ramp (color
    226, pure yellow, down to color 46, pure green) rather than a plain
    16-color code, since a one-shot color swap wouldn't read as a gradient."""
    clamped = min(max(percent, 0.0), 100.0)
    r = round(5 * (1 - clamped / 100))
    return f"\x1b[38;5;{46 + 36 * r}m"


def _mode_fragment(mode: str, paused: bool) -> str:
    fragment = mode
    if paused:
        fragment += "·paused"
    return fragment


def _allocate_chars(percentages: list[float], width: int) -> list[int]:
    """Largest-remainder rounding: converts float percentages (assumed to
    sum to ~100) into integer character counts that sum to exactly `width`.
    Rounding each share independently (e.g. int(x + 0.5)) can drift the
    total a character or two off `width`; this keeps every zone's width
    proportional while guaranteeing the bar itself always renders exactly
    `width` characters wide."""
    raw = [max(p, 0.0) / 100 * width for p in percentages]
    base = [int(x) for x in raw]
    remainder = width - sum(base)
    by_fraction = sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)
    for i in by_fraction[:remainder]:
        base[i] += 1
    return base


def _context_bar(
    squeezer_window_percent: float,
    human_window_percent: float,
    of_budget_percent: float,
    budget_of_window_percent: float,
    width: int = _CONTEXT_BAR_WIDTH,
) -> str:
    """The four-zone bar described above. squeezer's solid zone is clamped
    to its own allowed max (budget_of_window_percent) so an over-budget
    of_budget_percent (>100%, see _squeezer_usage_percents) fills that zone
    solid rather than overflowing into the human's zone."""
    allowed = min(max(budget_of_window_percent, 0.0), 100.0)
    squeezer_solid = min(max(squeezer_window_percent, 0.0), allowed)
    squeezer_headroom = allowed - squeezer_solid
    human_solid = min(max(human_window_percent, 0.0), 100.0 - allowed)
    tail = max(0.0, 100.0 - allowed - human_solid)

    a, b, c, d = _allocate_chars([squeezer_solid, squeezer_headroom, human_solid, tail], width)
    return (
        f"{_squeeze_color(of_budget_percent)}{'█' * a}{_ANSI_RESET}"
        f"{_ANSI_DIM_YELLOW}{'░' * b}{_ANSI_RESET}"
        f"{_ANSI_BRIGHT_BLUE}{'█' * c}{_ANSI_RESET}"
        f"{_ANSI_DIM}{'░' * d}{_ANSI_RESET}"
    )


def _squeezer_usage_fragments(
    squeezer_window_percent: float,
    human_window_percent: float,
    of_budget_percent: float,
    budget_of_window_percent: float,
) -> list[str]:
    bar = _context_bar(
        squeezer_window_percent, human_window_percent,
        of_budget_percent, budget_of_window_percent,
    )
    return [
        f"{bar} squeezed: {of_budget_percent:.0f}%",
        f"user: {human_window_percent:.0f}%",
        f"max: {budget_of_window_percent:.0f}% of the 5h window",
        f"total: {(squeezer_window_percent + human_window_percent):.0f}%",
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
    project_word = "project" if project_count == 1 else "projects"
    return f"{fragment} ({project_count} {project_word})"


def build_status_line(
    *,
    mode: str,
    paused: bool,
    squeezer_window_percent: float | None,
    human_window_percent: float | None,
    squeezer_budget_percent: float | None,
    squeezer_budget_of_window_percent: float | None,
    open_count: int,
    blocked_count: int,
    project_count: int,
    last_insight: str | None,
    width: int = 0,
) -> str:
    """Pure assembly of the ranked fragments (state -> squeezer's usage bar
    -> TODOs -> insight), truncated to `width` columns if given (0/falsy =
    no limit). The four squeezer_*/human_* percents come as a set — either
    all None (uncalibrated window, same fail-open convention as the rest of
    usage_lib) or all set — omitting the usage bar entirely rather than
    showing it as zero. All four are percentages of "the 5h window": the
    5-hour rolling token window squeezer draws its budget from (labeled
    plainly to avoid colliding with claude-hud's own "Usage"/"Context"
    bars — claude-hud's "Usage" is the account's real combined figure (this
    is squeezer's own accounting of the same window via transcript
    summation) and its "Context" is the unrelated current session's
    context-window fill, which resets on compaction). A None value
    (uncalibrated window, same fail-open convention as the rest of
    usage_lib — e.g. right after install, before squeezer has seen enough
    transcript activity to estimate the window total) renders as 0 rather
    than omitting the bar, so the HUD row is present immediately instead of
    only appearing once real data exists:
    - squeezer_window_percent: squeezer's own usage as a raw share of
      the 5h window — the width of the bar's solid squeeze-colored zone.
    - human_window_percent ("user: N%"): the human's own direct usage
      (outside squeezer) as a raw share of the 5h window — the bar's solid
      blue zone.
    - squeezer_budget_percent ("squeezed: N%"): squeezer's usage as a share
      of its own *allowed maximum* this window — how close squeezer is to
      the point it actually gets blocked, per usage_lib.budget_ok, and what
      colors the bar's solid zone along the yellow -> green gradient. Can
      exceed 100% if the human's own direct usage grew (shrinking the
      allowed maximum, see squeezer_budget_of_window_percent below) after
      squeezer had already used tokens against a larger one.
    - squeezer_budget_of_window_percent ("max: N% of the 5h window"):
      squeezer's allowed maximum as a share of the 5h window — (100% minus
      the configured reserve, 0 during no_reserve_hours) minus however much
      of it the human has already used directly, i.e. where the bar's
      squeeze zone (solid + dotted headroom) ends and the human's zone
      begins. Mirrors usage_lib.budget_ok's own gate (total_used <
      estimated_window_total * (1 - reserve%)) so squeezer_budget_percent
      hitting 100% lines up with the point budget_ok actually blocks
      squeezer, rather than measuring squeezer's usage against a reserve
      that ignores the human's own direct usage. Never below 0 (clamped for
      when the human alone has already crossed the threshold).
    A fourth text fragment, "total: N%", is squeezer_window_percent +
    human_window_percent — combined usage as a share of the 5h window."""
    parts = [_mode_fragment(mode, paused)]
    parts.extend(_squeezer_usage_fragments(
        squeezer_window_percent or 0.0, human_window_percent or 0.0,
        squeezer_budget_percent or 0.0, squeezer_budget_of_window_percent or 0.0,
    ))
    parts.append(_todo_fragment(open_count, blocked_count, project_count))
    if last_insight:
        parts.append(last_insight)
    line = "\U0001f34b " + " · ".join(parts)
    if width and _visible_len(line) > width:
        line = _truncate_visible(line, max(0, width - 1)) + _ANSI_RESET + "…"
    return line


def _squeezer_usage_percents(
    real_five_hour_percent: float | None = None,
) -> tuple[float, float, float, float] | None:
    """(squeezer_window, human_window, of_budget, budget_of_window) — see
    build_status_line's docstring for what each means — or None if the
    window hasn't been calibrated yet (same fail-open convention as the
    rest of usage_lib).

    total_used/squeezer_used come from usage_lib.total_used_since /
    sum_squeezer_usage_since, NOT find_known_transcript_path()/
    last_known_transcript_path: that pointer is overwritten by whichever
    session's PreToolUse hook fires last, including squeezer's own
    daemon-spawned turns, so right after squeezer runs a turn it stops
    meaning "total usage" and starts meaning "squeezer's own latest turn"
    alone — see total_used_since's own docstring for the fuller history.

    real_five_hour_percent: the account's real, server-reported 5-hour
    usage percentage (Claude Code's own stdin rate_limits.five_hour.
    used_percentage on a live statusLine render — see
    _real_five_hour_percent_from_stdin) — the same number /usage and
    claude-hud's own Usage bar show. squeezer's own estimated_window_total
    is just a local approximation (self-calibrated periodically against a
    /usage shell-out) and will naturally drift from that real figure. When
    given, it replaces squeezer's estimate as the ground truth for the
    total/"100%": squeezer's local transcript sums are used only to work
    out squeezer's *fraction* of this window's activity, which splits the
    real total into squeezer_window/human_window rather than each being
    independently estimated — so total (squeezer_window + human_window)
    always reconciles exactly to real_five_hour_percent, matching
    claude-hud. Also opportunistically feeds it into
    usage_lib.calibrate_window() so the actual budget gate self-corrects
    on every render this cheaply, instead of only via the periodic
    self_calibrate() timer (calibrate_window fails open — e.g. no known
    transcript yet — same as everywhere else, in which case this render
    just keeps the previously estimated_total). None (the default, and
    what current_status_line's non-statusLine callers — /squeezer:status,
    the Telegram header — always pass, since neither goes through Claude
    Code's statusLine stdin pipe) keeps today's fully self-estimated
    behavior unchanged.

    Calls maybe_roll_window() first so a statusLine render can self-heal an
    overdue window (e.g. left stale by a dead/missing daemon — see
    usage_lib.cmd_check's own call to it) even before any tool call in this
    session has fired the PreToolUse hook."""
    usage_lib.maybe_roll_window()
    state = usage_lib.load_state()
    if not state.get("calibrated"):
        return None
    total_used = usage_lib.total_used_since(state)
    squeezer_used = usage_lib.sum_squeezer_usage_since(state["window_start_ts"])
    human_used = total_used - squeezer_used
    estimated_total = state["estimated_window_total"]
    reserve_percent = usage_lib.load_reserve_percent()

    if real_five_hour_percent is not None:
        calibration = usage_lib.calibrate_window(real_five_hour_percent)
        if calibration.get("ok"):
            estimated_total = calibration["estimated_window_total"]
        fraction = squeezer_used / total_used if total_used else 0.0
        squeezer_window = real_five_hour_percent * fraction
        human_window = real_five_hour_percent - squeezer_window
    else:
        squeezer_window = 100 * squeezer_used / estimated_total
        human_window = 100 * human_used / estimated_total

    budget_of_window = max(0.0, (100 - reserve_percent) - human_window)
    allowed_max = estimated_total * budget_of_window / 100
    of_budget = 100 * squeezer_used / allowed_max if allowed_max else (100.0 if squeezer_used else 0.0)
    return squeezer_window, human_window, of_budget, budget_of_window


def _real_five_hour_percent_from_stdin() -> float | None:
    """Parses Claude Code's own statusLine JSON payload for
    rate_limits.five_hour.used_percentage — see _squeezer_usage_percents'
    docstring for what this is and why it's used. install_statusline.py's
    chain-wrapping (see its _wrap_command) gives every chained statusLine
    command, squeezer's own included, a full independent copy of this
    payload on stdin — without it, only the first command in a chained
    multi-line statusLine command actually receives any bytes (verified:
    the second of two sequential `cat`s in one shell invocation gets EOF
    immediately, since a pipe can't be read twice).

    None (fail open) if stdin is a live terminal (skip reading rather than
    blocking on a human running this by hand with nothing piped in), empty
    or unparseable (older Claude Code builds without rate_limits yet, or a
    statusLine command someone hand-edited to not pipe anything), or the
    field itself is missing or not a plain number."""
    if sys.stdin.isatty():
        return None
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = (payload.get("rate_limits") or {}).get("five_hour", {}).get("used_percentage")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value)


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


def current_status_line(width: int = None, real_five_hour_percent: float | None = None) -> str:
    """Gathers live state from SQUEEZER_HOME and assembles the line — the
    one entry point every caller (statusLine script, /squeezer:status,
    telegram_lib) actually uses. real_five_hour_percent is only ever passed
    by the statusLine __main__ entrypoint below (see
    _real_five_hour_percent_from_stdin) — the other two callers don't go
    through Claude Code's statusLine stdin pipe, so they leave it None and
    get squeezer's own self-estimated numbers, same as before this
    parameter existed."""
    cfg = _config.load_config()
    open_count, blocked_count, project_count = _todo_counts()
    usage_percents = _squeezer_usage_percents(real_five_hour_percent)
    (
        squeezer_window_percent, human_window_percent,
        squeezer_budget_percent, squeezer_budget_of_window_percent,
    ) = usage_percents if usage_percents else (None, None, None, None)
    return build_status_line(
        mode=cfg.get("mode", "auto"),
        paused=(_config.state_dir() / "paused").exists(),
        squeezer_window_percent=squeezer_window_percent,
        human_window_percent=human_window_percent,
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
        print(current_status_line(real_five_hour_percent=_real_five_hour_percent_from_stdin()))
    except Exception as e:  # never break a statusLine render / message send
        print(f"\U0001f34b squeezer: status unavailable ({e})")
