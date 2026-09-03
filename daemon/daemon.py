#!/usr/bin/env python3
"""squeezer's endless-loop background process — replaces tmux, orchestrator.py,
and telegram_bridge.py entirely. Runs standalone under an OS supervisor
(launchd/systemd, see install_service.py), NOT inside a Claude Code session.

Instead of keeping one interactive `claude` pane alive and typing into it via
tmux send-keys, this spawns one headless `claude -p --resume <session-id>`
process per turn — the session lives in Claude Code's own on-disk transcript,
resumed by id, so there's no pty to babysit and no paste-detection timing
hack. Four threads, coordinated only through SQUEEZER_HOME's state files and
one in-process work queue:

  - telegram_poll_loop: long-polls Telegram, handles /pause /resume /auto
    /manual directly, and queues everything else as work for the worker.
  - pacing_loop: decides, once per tick, whether fully-automatic or
    human-in-loop mode wants a continuation turn or a "what next" prompt
    right now (see daemon/human_in_loop.py for the mode's own branching).
  - self_calibrate_loop: periodic `claude -p "/usage"` recalibration
    (see usage_lib.self_calibrate).
  - worker_loop: the only thread that ever spawns `claude -p` — serialized
    through work_queue so two turns never race against the same session.
"""
import hashlib
import json
import queue
import subprocess
import sys
import threading
import time as time_mod
from datetime import datetime
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_mcp_deps  # noqa: E402
import config as _config  # noqa: E402
import human_in_loop  # noqa: E402
import telegram_lib  # noqa: E402
import usage_lib  # noqa: E402

PACING_INTERVAL = 30  # seconds between pacing ticks
SELF_CALIBRATE_INTERVAL = 20 * 60  # seconds, matches the old orchestrator's default
PAUSED_RECHECK_INTERVAL = 5 * 60  # seconds between checks for new todos/ content while loop-breaker-paused
NO_PROGRESS_LIMIT = 3  # consecutive stalled continuation turns before we pause and alert
CONTINUE_PROMPT = "Proceed to the next highest-priority item per todos/TODO.md."
CLAUDE_SPAWN_TIMEOUT = 60 * 60 * 4  # generous — a real turn can run long


class TelegramCommand(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    AUTO = "auto"
    MANUAL = "manual"
    MESSAGE = "message"


def classify_command(text: str) -> TelegramCommand:
    stripped = text.strip().lower()
    if stripped in ("/pause", "/stop"):
        return TelegramCommand.PAUSE
    if stripped in ("/resume", "/start", "/continue"):
        return TelegramCommand.RESUME
    if stripped == "/auto":
        return TelegramCommand.AUTO
    if stripped in ("/manual", "/human"):
        return TelegramCommand.MANUAL
    return TelegramCommand.MESSAGE


def build_claude_command(prompt: str, session_id: str | None, project_paths: list[str]) -> list[str]:
    cmd = ["claude", "-p", prompt, "--permission-mode", "auto", "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    for path in project_paths:
        cmd += ["--add-dir", path]
    return cmd


def compose_ack_message(busy: bool) -> str:
    """Instant Telegram reply sent the moment an ordinary message is queued —
    before any claude -p turn runs. A queued message otherwise sits silent
    until the (possibly very long, up to CLAUDE_SPAWN_TIMEOUT) current turn
    finishes, which reads as a hang; this makes sure every message gets an
    immediate response regardless of how busy the worker is."""
    if busy:
        return "Got it — still finishing up the current turn, I'll get to this right after."
    return "Got it — starting on this now."


def open_todo_summaries(todos_dir: Path, max_items: int = 5) -> list[str]:
    """Open (`- [ ]`) TODO lines across todos_dir/TODO.md and
    todos_dir/*/TODO.md — `- [b]` (blocked, awaiting a reply) is deliberately
    excluded, same convention CLAUDE.md's escalation flow already uses."""
    import re
    item_re = re.compile(r"^- \[ \] (.+)$")
    items = []
    if not todos_dir.exists():
        return items
    for path in sorted(todos_dir.rglob("TODO.md")):
        for line in path.read_text().splitlines():
            match = item_re.match(line.strip())
            if match:
                items.append(match.group(1).strip())
            if len(items) >= max_items:
                return items
    return items


def progress_signature(worklog_path: Path, todos_dir: Path) -> str:
    """Cheap fingerprint of worklog.md + every TODO.md's content — used to
    detect NO_PROGRESS_LIMIT stalled continuation turns in a row (the same
    "re-nagging a blocked item that wasn't marked `- [b]`" loop-breaker the
    old orchestrator.py had)."""
    h = hashlib.sha256()
    paths = sorted(todos_dir.rglob("TODO.md")) if todos_dir.exists() else []
    if worklog_path.exists():
        paths = [worklog_path] + paths
    for path in paths:
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()


def todos_signature(todos_dir: Path) -> str:
    """Cheap fingerprint of every TODO.md's content only — deliberately
    excludes worklog.md, unlike progress_signature above. progress_signature
    answers "did anything happen last turn" (worklog changing counts); this
    answers "is there new/changed *work*" for paused_recheck_loop, which
    only cares whether todos/ itself has moved since the loop-breaker paused
    — a worklog entry alone (e.g. a note-only turn) shouldn't count as new
    work worth waking up for."""
    h = hashlib.sha256()
    for path in sorted(todos_dir.rglob("TODO.md")) if todos_dir.exists() else []:
        h.update(path.read_bytes())
    return h.hexdigest()


class PausedRecheckAction(str, Enum):
    STAY_PAUSED = "stay_paused"  # nothing new since the pause, or already asked about this exact change
    AUTO_RESUME = "auto_resume"  # new/changed todos + nighttime -> lift the pause and let pacing_loop take over
    ASK = "ask"                  # new/changed todos + daytime -> ask before lifting the pause


def decide_paused_recheck_action(
    *,
    now: datetime,
    is_night: bool,
    todos_changed: bool,
    already_asked_for_current_signature: bool,
    snoozed_until: "datetime | None" = None,
) -> PausedRecheckAction:
    """Pure decision point for paused_recheck_loop's periodic check while
    the loop-breaker's own self-pause (alert_and_pause) is in effect —
    mirrors human_in_loop.decide_action's shape (pure, no I/O) for the same
    testability reason. Deliberately does NOT apply to a manual `/pause` —
    see paused_recheck_loop's own docstring for why that's scoped out.

    `snoozed_until`: set when a human explicitly declined the daytime ASK
    ("no, don't proceed") and was then asked when to check back — see
    save_paused_snooze's docstring for who sets this and why it isn't
    parsed here. Only suppresses the *daytime ASK* path: a still-active
    snooze does NOT block AUTO_RESUME, since night-time autonomy is a
    stronger, pre-existing guarantee than "don't disturb me during the
    day" and the two situations aren't in tension (the human declining a
    daytime ask says nothing about whether overnight auto-resume is
    welcome). Once `now >= snoozed_until`, the snooze is spent — this
    re-asks even if already_asked_for_current_signature is still true for
    the same unchanged todos content, since re-silencing after an expired
    snooze would defeat the entire point of asking "when should I check
    back" in the first place."""
    if not todos_changed:
        return PausedRecheckAction.STAY_PAUSED
    if is_night:
        return PausedRecheckAction.AUTO_RESUME
    if snoozed_until is not None:
        return PausedRecheckAction.STAY_PAUSED if now < snoozed_until else PausedRecheckAction.ASK
    if already_asked_for_current_signature:
        return PausedRecheckAction.STAY_PAUSED
    return PausedRecheckAction.ASK


# --- state persistence (SQUEEZER_HOME/state/*.json) ---

def _state_path(name: str) -> Path:
    return _config.state_dir() / name


def load_session_state() -> dict:
    path = _state_path("session.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass  # corrupt/truncated (e.g. write interrupted mid-flight) — fall back to default below
    return {"session_id": None}


def save_session_state(state: dict) -> None:
    _config.atomic_write_text(_state_path("session.json"), json.dumps(state, indent=2) + "\n")


def load_hil_state() -> dict:
    path = _state_path("human_in_loop.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass  # corrupt/truncated (e.g. write interrupted mid-flight) — fall back to default below
    return {"awaiting_reply": False, "last_asked_window_start": None, "last_asked_date": None,
            "budget_cap_percent": None, "cap_window_start_ts": None}


def save_hil_state(state: dict) -> None:
    _config.atomic_write_text(_state_path("human_in_loop.json"), json.dumps(state, indent=2) + "\n")


def load_totp_state() -> dict:
    path = _state_path("totp.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass  # corrupt/truncated (e.g. write interrupted mid-flight) — fall back to default below
    return {"last_used_step": None, "failed_attempts": [], "locked_until": None}


def save_totp_state(state: dict) -> None:
    _config.atomic_write_text(_state_path("totp.json"), json.dumps(state, indent=2) + "\n")


def load_elevation_state() -> dict:
    path = _state_path("elevation.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass  # corrupt/truncated (e.g. write interrupted mid-flight) — fall back to default below
    return {"expires_at": None}


def save_elevation_state(state: dict) -> None:
    _config.atomic_write_text(_state_path("elevation.json"), json.dumps(state, indent=2) + "\n")


def log(msg: str):
    print(f"[{time_mod.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# --- spawning claude -p ---

def spawn_claude(prompt: str) -> dict:
    """Runs one headless turn, resuming the last known session if any.
    Returns {"ok": bool, "session_id": str|None, "result": str|None,
    "error": str|None}. Never raises."""
    session_state = load_session_state()
    project_paths = [p["path"] for p in _config.projects()]
    cmd = build_claude_command(prompt, session_state.get("session_id"), project_paths)
    try:
        proc = subprocess.run(
            cmd, cwd=_config.squeezer_home(), capture_output=True, text=True,
            timeout=CLAUDE_SPAWN_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": False, "error": f"claude -p failed to run: {e}"}

    if proc.returncode != 0:
        return {"ok": False, "error": f"claude -p exited {proc.returncode}: {proc.stderr[-2000:]}"}

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"could not parse claude -p output: {proc.stdout[-2000:]}"}

    new_session_id = payload.get("session_id") or session_state.get("session_id")
    if new_session_id:
        save_session_state({"session_id": new_session_id})
    return {"ok": True, "session_id": new_session_id, "result": payload.get("result")}


# --- worker: the only thread that ever spawns claude -p ---

def worker_loop(work_queue: "queue.Queue[str]", stop_event: threading.Event, busy_event: threading.Event):
    last_signature = None
    no_progress_count = 0
    worklog_path = _config.state_dir() / "worklog.md"

    while not stop_event.is_set():
        try:
            prompt = work_queue.get(timeout=1)
        except queue.Empty:
            continue

        busy_event.set()
        try:
            log(f"spawning claude -p for: {prompt[:80]!r}")
            result = spawn_claude(prompt)
        finally:
            busy_event.clear()
        if not result["ok"]:
            log(f"claude -p failed: {result['error']}")
            continue

        cfg = _config.load_config()
        if cfg.get("telegram_verbosity") == "full" and result.get("result"):
            try:
                telegram_lib.send_message(result["result"])
            except Exception as e:  # noqa: BLE001 - never let a notify failure break the loop
                log(f"could not forward reply to telegram: {e}")

        if prompt == CONTINUE_PROMPT:
            sig = progress_signature(worklog_path, _config.todos_dir())
            if sig == last_signature:
                no_progress_count += 1
            else:
                no_progress_count = 0
            last_signature = sig
            if no_progress_count >= NO_PROGRESS_LIMIT:
                alert_and_pause(
                    f"{NO_PROGRESS_LIMIT} turns in a row with no change to todos/ or worklog.md "
                    "— likely re-nagging a blocked item that wasn't marked `- [b]`."
                )
                no_progress_count = 0


def alert_and_pause(reason: str):
    """Self-pause on the loop-breaker's own trigger (see worker_loop). Unlike
    a manual `/pause` (which just touches an empty file — see
    classify_command's PAUSE handling), this writes the todos-only
    fingerprint at the moment of pausing, so paused_recheck_loop can later
    tell "new work appeared" from "still the same stuck state" without
    re-triggering on the exact item that caused the pause in the first
    place."""
    info = {
        "source": "loop_breaker",
        "reason": reason,
        "paused_at": datetime.now().isoformat(),
        "todos_signature_at_pause": todos_signature(_config.todos_dir()),
        "asked_for_signature": None,
        "snoozed_until": None,
    }
    _config.atomic_write_text(_config.state_dir() / "paused", json.dumps(info, indent=2) + "\n")
    log(f"PAUSING (loop-breaker): {reason}")
    try:
        telegram_lib.send_message(f"Pausing myself: {reason} Check todos/TODO.md, then send /resume when it's clear to continue.")
    except Exception as e:  # noqa: BLE001
        log(f"could not send pause alert: {e}")


def is_paused() -> bool:
    return (_config.state_dir() / "paused").exists()


def load_paused_info() -> dict | None:
    """None if not paused. Otherwise the JSON `alert_and_pause` wrote
    (`source: "loop_breaker"`, plus its fingerprint/reason), or
    `{"source": "manual"}` for a plain `/pause` (an empty touched file,
    or any other non-JSON content) — paused_recheck_loop only acts on the
    former; see its own docstring for why a manual pause is left alone."""
    path = _config.state_dir() / "paused"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        return {"source": "manual"}


def save_paused_snooze(until: datetime) -> bool:
    """Sets (or clears, with a past `until`) the snooze deadline
    decide_paused_recheck_action's daytime ASK path respects. Deliberately
    NOT called from anywhere in this file — daemon.py has no natural-
    language time parser and shouldn't grow one; a human's "no, check me
    in 2 hours" reply is ordinary Telegram text, which the daemon already
    queues as work for a spawned `claude -p` turn (see classify_command's
    MESSAGE case) same as any other message. That spawned turn is what's
    expected to interpret the reply and call this — it has the full
    conversation context (it just asked the question), unlike this
    process. Returns False (no-op) if not currently loop-breaker-paused,
    since there's nothing to snooze."""
    info = load_paused_info()
    if info is None or info.get("source") != "loop_breaker":
        return False
    info["snoozed_until"] = until.isoformat()
    _config.atomic_write_text(_config.state_dir() / "paused", json.dumps(info, indent=2) + "\n")
    return True


# --- pacing: decides whether a continuation turn or a human-in-loop ask is due ---

def pacing_loop(work_queue: "queue.Queue[str]", stop_event: threading.Event):
    while not stop_event.wait(PACING_INTERVAL):
        try:
            _pacing_tick(work_queue)
        except Exception as e:  # noqa: BLE001 - one bad tick must not kill the loop
            log(f"pacing tick error (will retry next tick): {e}")


def _pacing_tick(work_queue: "queue.Queue[str]"):
    if is_paused():
        return
    if not open_todo_summaries(_config.todos_dir(), max_items=1):
        return  # nothing to do
    if not usage_lib.budget_ok():
        return  # reserve breached — idle until the window resets or a human takes over
    if not work_queue.empty():
        return  # a turn is already queued/running

    cfg = _config.load_config()
    mode = cfg.get("mode", "auto")

    if mode != "human_in_loop":
        work_queue.put(CONTINUE_PROMPT)
        return

    hil_cfg = cfg.get("human_in_loop", {})
    hil_state = load_hil_state()
    window_state = usage_lib.load_state()
    now = datetime.now()
    is_night = usage_lib.is_within_no_reserve_hours(now.time())
    no_reserve = cfg.get("no_reserve_hours")
    night_start = datetime.strptime(no_reserve["start"], "%H:%M").time() if no_reserve else None

    action = human_in_loop.decide_action(
        mode=mode,
        ask_cadence=hil_cfg.get("ask_cadence", "every_window_reset"),
        now=now,
        is_night=is_night,
        night_start=night_start,
        window_start_ts=window_state["window_start_ts"],
        state=hil_state,
        budget_cap_reached=_budget_cap_reached(hil_state, window_state),
    )

    if action == human_in_loop.Action.SEND_ASK:
        items = open_todo_summaries(_config.todos_dir())
        try:
            telegram_lib.send_message(human_in_loop.compose_ask_message(items))
        except Exception as e:  # noqa: BLE001
            log(f"could not send human-in-loop prompt: {e}")
            return
        hil_state["awaiting_reply"] = True
        hil_state["last_asked_window_start"] = window_state["window_start_ts"]
        hil_state["last_asked_date"] = now.date().isoformat()
        save_hil_state(hil_state)
    elif action == human_in_loop.Action.AUTO_CONTINUE:
        work_queue.put(CONTINUE_PROMPT)
    # IDLE: nothing to do this tick


def _budget_cap_reached(hil_state: dict, window_state: dict) -> bool:
    cap = hil_state.get("budget_cap_percent")
    if not cap or hil_state.get("cap_window_start_ts") != window_state.get("window_start_ts"):
        return False
    used = usage_lib.total_used_since(window_state)
    total = window_state.get("estimated_window_total") or usage_lib.DEFAULT_ESTIMATE
    return (used / total) * 100 >= cap


# --- paused_recheck: while loop-breaker-paused, periodically check for new
# todos/ content and either lift the pause (at night) or ask (during the
# day) — see decide_paused_recheck_action. Deliberately does NOT apply to a
# manual `/pause`: that's an explicit human "stop", and should stay stopped
# until an explicit `/resume`, not get silently lifted just because new
# work showed up. A tighter interval than PACING_INTERVAL isn't warranted —
# new todos items are a human/smart-mode-researcher action, not something
# that needs sub-minute latency to react to. ---

def paused_recheck_loop(stop_event: threading.Event):
    while not stop_event.wait(PAUSED_RECHECK_INTERVAL):
        try:
            _paused_recheck_tick()
        except Exception as e:  # noqa: BLE001 - one bad tick must not kill the loop
            log(f"paused recheck tick error (will retry next tick): {e}")


def _paused_recheck_tick():
    info = load_paused_info()
    if info is None or info.get("source") != "loop_breaker":
        return  # not paused, or a manual /pause — leave those alone

    current_sig = todos_signature(_config.todos_dir())
    now = datetime.now()
    snoozed_until = None
    if info.get("snoozed_until"):
        try:
            snoozed_until = datetime.fromisoformat(info["snoozed_until"])
        except ValueError:
            snoozed_until = None

    action = decide_paused_recheck_action(
        now=now,
        is_night=usage_lib.is_within_no_reserve_hours(now.time()),
        todos_changed=(current_sig != info.get("todos_signature_at_pause")),
        already_asked_for_current_signature=(info.get("asked_for_signature") == current_sig),
        snoozed_until=snoozed_until,
    )

    if action == PausedRecheckAction.AUTO_RESUME:
        (_config.state_dir() / "paused").unlink(missing_ok=True)
        log("auto-resuming (loop-breaker pause) — new todos/ content detected during no_reserve_hours")
        try:
            telegram_lib.send_message(
                "New work appeared in todos/ while I was paused (self-paused earlier by my "
                "loop-breaker) — resuming automatically since it's within no_reserve_hours. "
                "Send /pause if you'd rather I wait."
            )
        except Exception as e:  # noqa: BLE001
            log(f"could not send auto-resume notice: {e}")
    elif action == PausedRecheckAction.ASK:
        try:
            telegram_lib.send_message(
                "New work appeared in todos/ while I was paused (self-paused earlier by my "
                "loop-breaker). Send /resume to continue, or tell me when to check back "
                "(e.g. \"in 2 hours\", \"tomorrow morning\") and I'll wait until then instead "
                "of asking again right away."
            )
        except Exception as e:  # noqa: BLE001
            log(f"could not send paused-recheck ask: {e}")
            return
        info["asked_for_signature"] = current_sig
        info["snoozed_until"] = None  # any prior snooze has now been acted on (asked again)
        _config.atomic_write_text(_config.state_dir() / "paused", json.dumps(info, indent=2) + "\n")
    # STAY_PAUSED: nothing new, still within a snooze window, or already asked
    # about this exact change — stay quiet either way.


# --- telegram: handles pause/resume/mode instantly, queues everything else ---

def telegram_poll_loop(work_queue: "queue.Queue[str]", stop_event: threading.Event, busy_event: threading.Event):
    cfg = telegram_lib.TelegramConfig()
    log(f"telegram poll loop started, allowed chat_id={cfg.allowed_chat_id}")
    offset = 0
    while not stop_event.is_set():
        try:
            messages, offset = telegram_lib.get_updates(offset, cfg)
            for text in messages:
                _handle_telegram_message(text, cfg, work_queue, busy_event)
        except Exception as e:  # noqa: BLE001 - keep polling regardless
            log(f"error during telegram poll (will retry): {e}")
            time_mod.sleep(5)


def _handle_telegram_message(
    text: str, cfg: telegram_lib.TelegramConfig, work_queue: "queue.Queue[str]", busy_event: threading.Event
):
    command = classify_command(text)

    if command == TelegramCommand.PAUSE:
        (_config.state_dir() / "paused").touch()
        log("PAUSE requested — no new work will be injected until /resume")
        telegram_lib.send_message(
            "Paused. The current turn (if any) will finish, but no new work will be injected until /resume.", cfg
        )
        return

    if command == TelegramCommand.RESUME:
        (_config.state_dir() / "paused").unlink(missing_ok=True)
        log("RESUME requested")
        telegram_lib.send_message("Resumed.", cfg)
        return

    if command == TelegramCommand.AUTO:
        _config.set_mode("auto")
        log("switched to auto mode")
        telegram_lib.send_message("Switched to fully-automatic mode.", cfg)
        return

    if command == TelegramCommand.MANUAL:
        _config.set_mode("human_in_loop")
        log("switched to human_in_loop mode")
        telegram_lib.send_message("Switched to human-in-loop mode.", cfg)
        return

    # Ordinary message. If we're waiting on a human-in-loop reply, this is
    # it: pull out an optional budget cap, clear the wait, and let it
    # through to Claude either way — the daemon doesn't parse task intent,
    # Claude's own orchestration policy (CLAUDE.md) does.
    hil_state = load_hil_state()
    if hil_state.get("awaiting_reply"):
        cap = human_in_loop.parse_budget_cap(text)
        hil_state["awaiting_reply"] = False
        if cap is not None:
            hil_state["budget_cap_percent"] = cap
            hil_state["cap_window_start_ts"] = usage_lib.load_state()["window_start_ts"]
        save_hil_state(hil_state)

    try:
        telegram_lib.send_message(compose_ack_message(busy_event.is_set()), cfg)
    except Exception as e:  # noqa: BLE001 - a failed ack must not drop the message
        log(f"could not send ack: {e}")

    log(f"queuing human message: {text!r}")
    work_queue.put(f"[Telegram/User]: {text}")


# --- self-calibration timer ---

def self_calibrate_loop(stop_event: threading.Event):
    while not stop_event.wait(SELF_CALIBRATE_INTERVAL):
        result = usage_lib.self_calibrate()
        if not result.get("ok"):
            log(f"self-calibrate failed (will retry next interval): {result.get('error')}")
        rolled = usage_lib.maybe_roll_window()
        if rolled:
            log(f"window rolled: start={rolled['window_start_ts']} estimated_total={rolled['estimated_window_total']}")


def main():
    _config.state_dir()  # ensure it exists
    work_queue: "queue.Queue[str]" = queue.Queue()
    stop_event = threading.Event()
    busy_event = threading.Event()

    threads = [
        threading.Thread(target=telegram_poll_loop, args=(work_queue, stop_event, busy_event), daemon=True),
        threading.Thread(target=pacing_loop, args=(work_queue, stop_event), daemon=True),
        threading.Thread(target=self_calibrate_loop, args=(stop_event,), daemon=True),
        threading.Thread(target=worker_loop, args=(work_queue, stop_event, busy_event), daemon=True),
        threading.Thread(target=paused_recheck_loop, args=(stop_event,), daemon=True),
    ]
    for t in threads:
        t.start()

    if not check_mcp_deps.is_importable(sys.executable):
        log(
            "WARNING: the mcp Python package is not importable by "
            f"{sys.executable} — the squeezer-telegram MCP server (telegram_send) "
            "will fail to start, so spawned turns have no way to reply on Telegram. "
            "Re-run `/squeezer:setup` (step 8, check_mcp_deps.py) to fix."
        )

    log("squeezer daemon started")
    try:
        while True:
            time_mod.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()
        for t in threads:
            t.join(timeout=5)


def cmd_snooze(until_iso: str) -> None:
    """`python3 daemon.py snooze <ISO-8601 timestamp>` — the one-shot CLI a
    spawned `claude -p` turn runs after interpreting a human's "no, check
    me back at <time>" reply to the paused_recheck_loop daytime ASK (see
    save_paused_snooze's docstring for why the parsing happens in that
    turn, not here). Not meant to be run by a human directly; the
    ISO-formatted arg is what the turn is expected to compute from
    whatever natural-language time the human actually said."""
    try:
        until = datetime.fromisoformat(until_iso)
    except ValueError:
        print(f"not a valid ISO-8601 timestamp: {until_iso!r}", file=sys.stderr)
        sys.exit(1)
    if save_paused_snooze(until):
        print(f"snoozed paused-recheck asks until {until.isoformat()}")
    else:
        print("not currently loop-breaker-paused — nothing to snooze", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "snooze":
        if len(sys.argv) < 3:
            print("usage: daemon.py snooze <ISO-8601 timestamp>", file=sys.stderr)
            sys.exit(1)
        cmd_snooze(sys.argv[2])
    else:
        main()
