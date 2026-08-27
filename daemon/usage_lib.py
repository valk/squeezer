#!/usr/bin/env python3
"""Shared token-usage accounting, used by hooks/budget_guard.sh (as a PreToolUse
hook) and daemon/daemon.py (to roll the window on reset, and to self-calibrate
periodically).

There's no published exact token quota for a Pro-plan 5-hour window. It was
originally assumed there was no non-interactive way to read the real number
Claude Code's own /usage command shows either (only checked: no `claude usage`
subcommand, no local file it writes) — but `claude -p "/usage"` turns out to
work fine: /usage is handled client-side and never reaches the model, so a
print-mode invocation returns the same numbers the interactive command shows,
at negligible cost (see self_calibrate() below, added 2026-08-23). Until this
has been calibrated at least once against a real /usage reading, the reserve
check FAILS OPEN (always allows) rather than guessing and potentially blocking
work at a fraction of the real limit. Calibrate manually with:
    python3 daemon/usage_lib.py calibrate <percent-shown-by-/usage>
or automatically (both window and weekly trackers in one shot) with:
    python3 daemon/usage_lib.py self-calibrate
`daemon/daemon.py` runs the latter on a timer so this normally never needs a
human in the loop. After that, the window estimate self-corrects further as
real windows complete (tracked in SQUEEZER_HOME/state/window_budget.json).
"""
import json
import re
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as _config  # noqa: E402

STATE_PATH = _config.squeezer_home() / "state" / "window_budget.json"
DEFAULT_ESTIMATE = 2_000_000  # conservative placeholder until real windows are observed

# --- Weekly pacing (separate from the 5-hour window above) ---
#
# Smart mode (see CLAUDE.md) proposes and builds its own work when a project's
# TODO is empty. The user wants that automated work to land close to 100% of the
# *weekly* token quota right as the week resets, without spiking early and
# then sitting idle. There's no scriptable way to read the real weekly % used
# either (same limitation as the 5-hour window) — only /usage shows it. Unlike
# the 5-hour tracker, this can't sum a live transcript since the daemon spawns
# a fresh headless `claude -p` process per turn, which would fragment any
# single-session token sum across a whole week. So this tracks pure
# calibration checkpoints instead: the user reads both "% of weekly quota used"
# and "hours until weekly reset" off the same /usage screen and feeds them in
# via `calibrate-week`; everything else is computed from the resulting
# deadline, self-correcting on every re-calibration.
WEEKLY_STATE_PATH = _config.squeezer_home() / "state" / "weekly_budget.json"
WEEKLY_PERIOD = timedelta(days=7)
# smart_mode_gate() bands: below BEHIND -> full allotment (there's headroom to
# spend), above AHEAD -> skip this cycle (fall back to ROUTINE.md), between
# the two -> half allotment (keep making progress without spending it all
# while already on pace). Plain constants so the user can retune them directly.
SMART_GATE_BEHIND_THRESHOLD = 0.85
SMART_GATE_AHEAD_THRESHOLD = 1.15


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    state = {
        "window_start_ts": now_iso(),
        "estimated_window_total": DEFAULT_ESTIMATE,
        "past_window_totals": [],
        "calibrated": False,
        "squeezer_transcript_paths": [],
    }
    save_state(state)
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def is_within_no_reserve_hours(now: time | None = None) -> bool:
    """Whether the current local time falls inside the configured
    no-reserve window — hours where no one needs the reserve free to grab
    manual control (e.g. overnight), so the full token budget can be used.
    Also reused, unchanged, as the night-suppression window for human-in-loop
    mode (see daemon/human_in_loop.py) — one config knob, two consumers."""
    hours = _config.load_config().get("no_reserve_hours")
    if not hours:
        return False
    start = datetime.strptime(hours["start"], "%H:%M").time()
    end = datetime.strptime(hours["end"], "%H:%M").time()
    now = now if now is not None else datetime.now().time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # window wraps past midnight


def load_reserve_percent() -> float:
    """Returns 0 during the configured no_reserve_hours window, since no
    reserve is needed then."""
    if is_within_no_reserve_hours():
        return 0
    return _config.load_config().get("reserve_percent", 20)


def load_weekly_state():
    if WEEKLY_STATE_PATH.exists():
        with open(WEEKLY_STATE_PATH) as f:
            return json.load(f)
    state = {
        "last_calibrated_percent": 0,
        "reset_ts": None,
        "calibrated_at": None,
        "calibrated": False,
    }
    save_weekly_state(state)
    return state


def save_weekly_state(state):
    WEEKLY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEEKLY_STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def calibrate_weekly(percent: float, hours_until_reset: float) -> dict:
    """Non-exiting core of weekly calibration, shared by cmd_calibrate_week
    (human-relayed numbers) and self_calibrate (parsed from `claude -p
    "/usage"`) — returns {"ok": False, "error": ...} on bad input instead of
    exiting, since self_calibrate runs unattended in a background thread."""
    if not (0 <= percent <= 100):
        return {"ok": False, "error": "percent must be between 0 and 100"}
    if hours_until_reset < 0:
        return {"ok": False, "error": "hours_until_reset must be >= 0"}

    now = datetime.now(timezone.utc)
    state = load_weekly_state()
    state["last_calibrated_percent"] = percent
    state["reset_ts"] = (now + timedelta(hours=hours_until_reset)).isoformat()
    state["calibrated_at"] = now.isoformat()
    state["calibrated"] = True
    save_weekly_state(state)
    return {"ok": True, "reset_ts": state["reset_ts"]}


def cmd_calibrate_week(percent: float, hours_until_reset: float):
    """Weekly analogue of cmd_calibrate: the user reads both numbers straight off
    the same /usage screen (weekly % used, hours until weekly reset)."""
    result = calibrate_weekly(percent, hours_until_reset)
    if not result["ok"]:
        print(result["error"], file=sys.stderr)
        sys.exit(1)
    print(f"weekly calibrated: {percent}% used, resets at {result['reset_ts']}")


def weekly_pace_ratio() -> float | None:
    """<1 = behind pace (headroom to spend more), ~1 = on pace, >1 = ahead of
    pace (throttle). None if never calibrated (fail-open, like the rest of
    this module)."""
    state = load_weekly_state()
    if not state.get("calibrated"):
        return None
    reset_ts = datetime.fromisoformat(state["reset_ts"])
    remaining = reset_ts - datetime.now(timezone.utc)
    elapsed_fraction = 1 - (remaining / WEEKLY_PERIOD)
    # Clamp: freshly calibrated (elapsed ~0) must not divide by ~0, and a
    # reset_ts already in the past shouldn't produce >1 elapsed either.
    elapsed_fraction = min(1.0, max(0.01, elapsed_fraction))
    return (state["last_calibrated_percent"] / 100) / elapsed_fraction


def smart_mode_gate(tasks_per_cycle: int = 3) -> dict:
    """The single decision point smart mode consults before starting a
    research/build cycle. Pure function of weekly_pace_ratio() plus the
    project's configured tasks_per_cycle — see SMART_GATE_*_THRESHOLD above."""
    ratio = weekly_pace_ratio()
    if ratio is None or ratio < SMART_GATE_BEHIND_THRESHOLD:
        return {"proceed": True, "tasks_this_cycle": tasks_per_cycle, "ratio": ratio}
    if ratio > SMART_GATE_AHEAD_THRESHOLD:
        return {"proceed": False, "ratio": ratio, "reason": f"ahead of weekly pace (ratio {ratio:.2f})"}
    return {"proceed": True, "tasks_this_cycle": max(1, tasks_per_cycle // 2), "ratio": ratio}


def load_smart_mode_config(project_name: str = None) -> dict:
    """A named project's own smart_mode block overrides individual keys on
    top of the top-level default."""
    config = _config.load_config()
    result = dict(config.get("smart_mode", {"enabled": True, "tasks_per_cycle": 3}))
    if project_name:
        for p in config.get("projects", []):
            if p.get("name") == project_name:
                result.update(p.get("smart_mode", {}))
                break
    return result


def cmd_smart_gate():
    project_name = sys.argv[2] if len(sys.argv) > 2 else None
    smart_config = load_smart_mode_config(project_name)
    if not smart_config.get("enabled", True):
        print(json.dumps({"proceed": False, "reason": "smart_mode disabled for this project"}, indent=2))
        return
    print(json.dumps(smart_mode_gate(smart_config.get("tasks_per_cycle", 3)), indent=2))


def sum_usage_since(transcript_path: str, since_ts: str) -> int:
    """Sum input+output+cache_creation tokens (cache_read weighted at 10%,
    since it's heavily discounted and barely touches the usage-limit budget)
    across assistant turns in the transcript at/after since_ts."""
    since = datetime.fromisoformat(since_ts)
    total = 0
    path = Path(transcript_path)
    if not path.exists():
        return 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = entry.get("timestamp")
            if not ts:
                continue
            try:
                entry_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if entry_ts < since:
                continue
            usage = (entry.get("message") or {}).get("usage") or entry.get("usage")
            if not usage:
                continue
            total += usage.get("input_tokens", 0)
            total += usage.get("output_tokens", 0)
            total += usage.get("cache_creation_input_tokens", 0)
            total += int(usage.get("cache_read_input_tokens", 0) * 0.1)
    return total


def sum_squeezer_usage_since(since_ts: str) -> int:
    """Sum tokens across every transcript belonging to squeezer's own
    daemon-spawned turns since since_ts (see cmd_check's squeezer_transcript_paths
    tracking, keyed on cwd == squeezer_home()) — distinct from sum_usage_since's
    single "last known" transcript, which could belong to any session."""
    state = load_state()
    return sum(sum_usage_since(p, since_ts) for p in state.get("squeezer_transcript_paths", []))


def total_used_since(state: dict) -> int:
    """Total tokens used against the window budget since state["window_start_ts"],
    combining the human's own session (last_known_human_transcript_path) with
    every squeezer daemon-spawned turn (squeezer_transcript_paths).

    Deliberately does NOT use last_known_transcript_path/find_known_transcript_path
    for this: that pointer is overwritten by whichever session's PreToolUse hook
    fires last, including squeezer's own daemon-spawned turns (see cmd_check).
    Once squeezer runs a turn, "last known" stops meaning "total usage" and starts
    meaning "squeezer's latest turn alone" — which previously made hud_status's
    of_used bar (squeezer_used / that same narrow transcript) jump to ~100%, and
    climb past 100% once squeezer_used summed multiple daemon turns against a
    denominator that only ever reflected the single most recent one."""
    since_ts = state["window_start_ts"]
    human_path = state.get("last_known_human_transcript_path")
    human_used = sum_usage_since(human_path, since_ts) if human_path else 0
    squeezer_used = sum(sum_usage_since(p, since_ts) for p in state.get("squeezer_transcript_paths", []))
    return human_used + squeezer_used


def budget_ok(transcript_path: str = None) -> bool:
    """Whether the reserve is NOT yet breached for the current window — the
    same check cmd_check() enforces per tool call, exposed as a plain query
    for daemon.py's own pacing decision (spawn another turn at all?) rather
    than a specific PreToolUse payload. Fails open (True) if the estimate
    has never been calibrated. With an explicit transcript_path (cmd_check's
    live gate on the current squeezer turn), checks just that transcript;
    otherwise checks total_used_since — human + every squeezer turn so far
    this window — since daemon.py's pacing check has no single transcript
    of its own to hand in."""
    state = load_state()
    if not state.get("calibrated"):
        return True
    used = (
        sum_usage_since(transcript_path, state["window_start_ts"])
        if transcript_path else total_used_since(state)
    )
    threshold = state["estimated_window_total"] * (1 - load_reserve_percent() / 100)
    return used < threshold


def _is_squeezer_cwd(cwd: str | None) -> bool:
    """Whether a PreToolUse payload's cwd belongs to squeezer's own
    daemon-spawned turn — daemon.spawn_claude always runs `claude -p` with
    cwd=squeezer_home() — as opposed to an interactive session or an agent
    working on some other project."""
    if not cwd:
        return False
    try:
        return Path(cwd).resolve() == _config.squeezer_home().resolve()
    except OSError:
        return False


def cmd_check():
    """Read a PreToolUse hook payload from stdin, block only if the payload
    belongs to squeezer's own daemon-spawned turn AND the reserve is
    breached. This hook is chained globally into every Claude Code session,
    but the reserve exists to protect a human's own quota, not gate it — an
    interactive session or an agent working on some other project must
    never be denied a tool call by squeezer's own budget.

    Also persists transcript_path to state as a side effect — last_known_
    transcript_path is the shared source of truth other commands (calibrate,
    roll-window) fall back to when they don't have one of their own,
    regardless of whose session it was. For squeezer's own turns
    specifically, the transcript is additionally appended to
    squeezer_transcript_paths; for every other (human/other-project) turn,
    it's also recorded as last_known_human_transcript_path — kept apart from
    last_known_transcript_path so it can't be clobbered by squeezer's own
    turns, since total_used_since() (hud_status's usage bars, and the
    daemon's own budget pacing) needs a "total usage" pointer that survives
    squeezer running its own turns.

    Also opportunistically rolls an overdue window (see maybe_roll_window)
    before reading state: this hook fires on every tool call in every
    session, human or squeezer, independent of whether the daemon's own
    self_calibrate_loop timer is even running — so it catches a stale window
    left behind by a dead/missing daemon far sooner than waiting for that
    timer's next 20-minute tick after the daemon comes back.
    """
    maybe_roll_window()
    payload = json.load(sys.stdin)
    transcript_path = payload.get("transcript_path")
    is_squeezer_turn = _is_squeezer_cwd(payload.get("cwd"))
    state = load_state()

    if transcript_path:
        state["last_known_transcript_path"] = transcript_path
        if is_squeezer_turn:
            paths = state.setdefault("squeezer_transcript_paths", [])
            if transcript_path not in paths:
                paths.append(transcript_path)
        else:
            state["last_known_human_transcript_path"] = transcript_path
        save_state(state)

    if is_squeezer_turn and transcript_path and not budget_ok(transcript_path):
        used = sum_usage_since(transcript_path, state["window_start_ts"])
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Token budget reserve ({load_reserve_percent()}%) reached for this window "
                    f"({used}/{state['estimated_window_total']} est. tokens used, estimate last "
                    f"calibrated against /usage). Going idle until the window resets or a human "
                    f"takes over. If /usage disagrees with this, recalibrate: "
                    f"python3 daemon/usage_lib.py calibrate <percent-shown-by-/usage>"
                ),
            }
        }))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))


def find_known_transcript_path() -> str | None:
    """Best-known current transcript path, kept fresh by every PreToolUse
    check — regardless of whose session it was. See find_known_human_
    transcript_path for the variant that excludes squeezer's own turns."""
    state = load_state()
    return state.get("last_known_transcript_path")


def find_known_human_transcript_path() -> str | None:
    """Best-known transcript path for a non-squeezer (human or other-project)
    session — unlike find_known_transcript_path, never overwritten by
    squeezer's own daemon-spawned turns, so it stays a reliable stand-in for
    "the human's own session total" even right after squeezer runs a turn.
    See total_used_since()."""
    state = load_state()
    return state.get("last_known_human_transcript_path")


def calibrate_window(real_percent: float, transcript_path: str = None) -> dict:
    """Non-exiting core of window calibration, shared by cmd_calibrate
    (human-relayed number) and self_calibrate (parsed from `claude -p
    "/usage"`) — returns {"ok": False, "error": ...} on bad input instead of
    exiting, since self_calibrate runs unattended in a background thread."""
    if not (0 < real_percent <= 100):
        return {"ok": False, "error": "real_percent must be between 0 and 100"}

    transcript_path = transcript_path or find_known_transcript_path()
    if not transcript_path:
        return {"ok": False, "error": "no transcript_path given, and none known yet (no PreToolUse "
                "hook has fired this window) — run a tool call first, then retry"}

    state = load_state()
    used = sum_usage_since(transcript_path, state["window_start_ts"])
    state["estimated_window_total"] = int(used / (real_percent / 100))
    state["calibrated"] = True
    save_state(state)
    return {"ok": True, "used": used, "estimated_window_total": state["estimated_window_total"]}


def cmd_calibrate(real_percent: float, transcript_path: str = None):
    """Correct the estimate using a real reading from Claude Code's own
    /usage command."""
    result = calibrate_window(real_percent, transcript_path)
    if not result["ok"]:
        print(result["error"], file=sys.stderr)
        sys.exit(1)
    print(f"calibrated: {result['used']} tokens = {real_percent}% -> "
          f"estimated_window_total={result['estimated_window_total']}")


# --- Self-calibration: shell out to `claude -p "/usage"` directly ---
#
# Discovered 2026-08-23: contrary to this module's original assumption, print-
# mode `claude -p "/usage"` returns the same numbers the interactive /usage
# command shows. /usage is rendered client-side from locally-tracked usage
# data and never reaches the model, so this costs no meaningful tokens against
# the very quota it's reading. That makes it safe to poll on a timer instead
# of relying on a human to relay numbers.
_USAGE_LINE_RE = re.compile(
    r"Current (session|week)[^:\n]*:\s*([\d.]+)%\s*used.*?"
    r"resets\s+([A-Za-z]+\s+\d{1,2})\s+at\s+(\d{1,2}(?::\d{2})?\s*[ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


def parse_usage_output(text: str, now: datetime = None) -> dict:
    """Parse `claude -p "/usage"` stdout into calibration-ready numbers:
    {"session_percent", "session_hours_until_reset", "week_percent",
    "week_hours_until_reset"}. Raises ValueError if either line isn't found
    (e.g. the CLI's /usage format changed) rather than calibrating on
    garbage."""
    now = now or datetime.now(timezone.utc)
    result = {}
    for match in _USAGE_LINE_RE.finditer(text):
        kind, percent, month_day, time_str, tz_name = match.groups()
        tz = ZoneInfo(tz_name)
        now_local = now.astimezone(tz)
        time_str = time_str.strip().lower().replace(" ", "")
        time_fmt = "%I:%M%p" if ":" in time_str else "%I%p"
        reset_local = datetime.strptime(
            f"{month_day} {now_local.year} {time_str}", f"%b %d %Y {time_fmt}"
        ).replace(tzinfo=tz)
        if reset_local < now_local:
            reset_local = reset_local.replace(year=reset_local.year + 1)
        hours_until = (reset_local.astimezone(timezone.utc) - now).total_seconds() / 3600
        key = "session" if kind.lower() == "session" else "week"
        result[f"{key}_percent"] = float(percent)
        result[f"{key}_hours_until_reset"] = hours_until

    missing = {"session_percent", "week_percent"} - result.keys()
    if missing:
        raise ValueError(f"could not parse /usage output (missing {sorted(missing)}): {text!r}")
    return result


def self_calibrate(timeout: int = 60) -> dict:
    """Run `claude -p "/usage"`, parse it, and calibrate both the window and
    weekly trackers from the real numbers — no human relay needed. Safe to
    call unattended (e.g. from daemon.py's timer): never raises, always
    returns a result dict with "ok" set."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "/usage"],
            capture_output=True, text=True, timeout=timeout, check=True,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": False, "error": f"claude -p /usage failed to run: {e}"}

    try:
        parsed = parse_usage_output(proc.stdout)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    window_result = calibrate_window(parsed["session_percent"])
    week_result = calibrate_weekly(parsed["week_percent"], parsed["week_hours_until_reset"])

    return {
        "ok": window_result["ok"] and week_result["ok"],
        "session_percent": parsed["session_percent"],
        "window": window_result,
        "week_percent": parsed["week_percent"],
        "week_hours_until_reset": round(parsed["week_hours_until_reset"], 2),
        "week": week_result,
    }


def cmd_self_calibrate():
    result = self_calibrate()
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        sys.exit(1)


def roll_window(final_transcript_path: str = None) -> dict:
    """Non-exiting core of window rolling, shared by cmd_roll_window (manual
    CLI) and maybe_roll_window (automatic, timer-driven): records the
    just-finished window's total, recomputes the rolling estimate from
    recent window history, and starts a fresh window."""
    state = load_state()
    if final_transcript_path:
        final_total = sum_usage_since(final_transcript_path, state["window_start_ts"])
        state["past_window_totals"].append(final_total)
        state["past_window_totals"] = state["past_window_totals"][-10:]  # keep recent history
        if state["past_window_totals"]:
            state["estimated_window_total"] = int(sum(state["past_window_totals"]) / len(state["past_window_totals"]))
    state["window_start_ts"] = now_iso()
    state["squeezer_transcript_paths"] = []
    save_state(state)
    return {"window_start_ts": state["window_start_ts"], "estimated_window_total": state["estimated_window_total"]}


def cmd_roll_window(final_transcript_path: str = None):
    """Called by the daemon when it detects the window has reset: record the
    just-finished window's total, recompute the rolling estimate, reset."""
    result = roll_window(final_transcript_path)
    print(f"window rolled: start={result['window_start_ts']} estimated_total={result['estimated_window_total']}")


WINDOW_PERIOD = timedelta(hours=5)


def maybe_roll_window(now: datetime | None = None) -> dict | None:
    """Automatic counterpart to the manual `roll-window` CLI: rolls the
    window once WINDOW_PERIOD has actually elapsed since window_start_ts.
    Nothing else in the codebase ever detected a real window reset despite
    cmd_roll_window's own docstring assuming a caller like this existed —
    daemon.py's self_calibrate_loop only ever refreshed
    estimated_window_total, never window_start_ts, so it grew stale
    indefinitely (skewing both that estimate and the reserve gate, both of
    which measure "used since window_start_ts"). Call this periodically
    (self_calibrate_loop, alongside self_calibrate()) instead.

    Uses find_known_transcript_path() (the most recently seen transcript,
    regardless of whose turn it was — same fallback calibrate_window()
    already uses) as the just-finished window's stand-in for
    past_window_totals. Returns roll_window()'s result if it rolled, None
    if the window's still current — including right after this state was
    first created, since load_state() seeds window_start_ts to now."""
    now = now or datetime.now(timezone.utc)
    state = load_state()
    window_start = datetime.fromisoformat(state["window_start_ts"])
    if now - window_start < WINDOW_PERIOD:
        return None
    return roll_window(find_known_transcript_path())


def cmd_status():
    state = load_state()
    print(json.dumps(state, indent=2))


def cmd_quiet_hours():
    """Whether we're inside the configured no_reserve_hours window right now
    — reused by smart-developer as the night auto-decide trigger, and by
    daemon/human_in_loop.py as the human-in-loop night-suppression trigger."""
    print(json.dumps({"quiet_hours": is_within_no_reserve_hours()}))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "usage: usage_lib.py {check|roll-window [transcript_path]|status|"
            "calibrate <percent> [transcript_path]|"
            "calibrate-week <percent> <hours-until-reset>|self-calibrate|"
            "smart-gate [project-name]|quiet-hours}",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "check":
        cmd_check()
    elif cmd == "roll-window":
        cmd_roll_window(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "status":
        cmd_status()
    elif cmd == "calibrate":
        if len(sys.argv) < 3:
            print("usage: usage_lib.py calibrate <percent-shown-by-/usage> [transcript_path]", file=sys.stderr)
            sys.exit(1)
        cmd_calibrate(float(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "calibrate-week":
        if len(sys.argv) < 4:
            print("usage: usage_lib.py calibrate-week <percent-shown-by-/usage> <hours-until-weekly-reset>", file=sys.stderr)
            sys.exit(1)
        cmd_calibrate_week(float(sys.argv[2]), float(sys.argv[3]))
    elif cmd == "self-calibrate":
        cmd_self_calibrate()
    elif cmd == "smart-gate":
        cmd_smart_gate()
    elif cmd == "quiet-hours":
        cmd_quiet_hours()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
