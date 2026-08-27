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
import config as _config  # noqa: E402
import human_in_loop  # noqa: E402
import telegram_lib  # noqa: E402
import usage_lib  # noqa: E402

PACING_INTERVAL = 30  # seconds between pacing ticks
SELF_CALIBRATE_INTERVAL = 20 * 60  # seconds, matches the old orchestrator's default
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


# --- state persistence (SQUEEZER_HOME/state/*.json) ---

def _state_path(name: str) -> Path:
    return _config.state_dir() / name


def load_session_state() -> dict:
    path = _state_path("session.json")
    if path.exists():
        return json.loads(path.read_text())
    return {"session_id": None}


def save_session_state(state: dict) -> None:
    _state_path("session.json").write_text(json.dumps(state, indent=2) + "\n")


def load_hil_state() -> dict:
    path = _state_path("human_in_loop.json")
    if path.exists():
        return json.loads(path.read_text())
    return {"awaiting_reply": False, "last_asked_window_start": None, "last_asked_date": None,
            "budget_cap_percent": None, "cap_window_start_ts": None}


def save_hil_state(state: dict) -> None:
    _state_path("human_in_loop.json").write_text(json.dumps(state, indent=2) + "\n")


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

def worker_loop(work_queue: "queue.Queue[str]", stop_event: threading.Event):
    last_signature = None
    no_progress_count = 0
    worklog_path = _config.state_dir() / "worklog.md"

    while not stop_event.is_set():
        try:
            prompt = work_queue.get(timeout=1)
        except queue.Empty:
            continue

        log(f"spawning claude -p for: {prompt[:80]!r}")
        result = spawn_claude(prompt)
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
    (_config.state_dir() / "paused").touch()
    log(f"PAUSING (loop-breaker): {reason}")
    try:
        telegram_lib.send_message(f"Pausing myself: {reason} Check todos/TODO.md, then send /resume when it's clear to continue.")
    except Exception as e:  # noqa: BLE001
        log(f"could not send pause alert: {e}")


def is_paused() -> bool:
    return (_config.state_dir() / "paused").exists()


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


# --- telegram: handles pause/resume/mode instantly, queues everything else ---

def telegram_poll_loop(work_queue: "queue.Queue[str]", stop_event: threading.Event):
    cfg = telegram_lib.TelegramConfig()
    log(f"telegram poll loop started, allowed chat_id={cfg.allowed_chat_id}")
    offset = 0
    while not stop_event.is_set():
        try:
            messages, offset = telegram_lib.get_updates(offset, cfg)
            for text in messages:
                _handle_telegram_message(text, cfg, work_queue)
        except Exception as e:  # noqa: BLE001 - keep polling regardless
            log(f"error during telegram poll (will retry): {e}")
            time_mod.sleep(5)


def _handle_telegram_message(text: str, cfg: telegram_lib.TelegramConfig, work_queue: "queue.Queue[str]"):
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

    log(f"queuing human message: {text!r}")
    work_queue.put(f"[Telegram/User]: {text}")


# --- self-calibration timer ---

def self_calibrate_loop(stop_event: threading.Event):
    while not stop_event.wait(SELF_CALIBRATE_INTERVAL):
        result = usage_lib.self_calibrate()
        if not result.get("ok"):
            log(f"self-calibrate failed (will retry next interval): {result.get('error')}")


def main():
    _config.state_dir()  # ensure it exists
    work_queue: "queue.Queue[str]" = queue.Queue()
    stop_event = threading.Event()

    threads = [
        threading.Thread(target=telegram_poll_loop, args=(work_queue, stop_event), daemon=True),
        threading.Thread(target=pacing_loop, args=(work_queue, stop_event), daemon=True),
        threading.Thread(target=self_calibrate_loop, args=(stop_event,), daemon=True),
        threading.Thread(target=worker_loop, args=(work_queue, stop_event), daemon=True),
    ]
    for t in threads:
        t.start()

    log("squeezer daemon started")
    try:
        while True:
            time_mod.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()
        for t in threads:
            t.join(timeout=5)


if __name__ == "__main__":
    main()
