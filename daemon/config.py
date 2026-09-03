#!/usr/bin/env python3
"""Resolves and reads squeezer's per-install control directory (SQUEEZER_HOME)
— separate from wherever the plugin package itself is installed, so
reinstalling/upgrading the plugin never touches your registered projects,
secrets, or state. Defaults to ~/.config/squeezer; override with the
SQUEEZER_HOME env var. Populated by the `/squeezer:setup` command from the
plugin's bundled templates/ on first run.
"""
import json
import os
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Writes `text` to `path` without ever leaving a truncated/partial file
    behind if the process is killed mid-write: writes to a sibling temp file
    first, then atomically renames it into place. Plain Path.write_text()
    truncates the destination before writing, so a process killed at exactly
    the wrong moment (e.g. daemon restart, OOM) leaves a 0-byte file — every
    json.load() of that file forever after fails with "Expecting value:
    line 1 column 1 (char 0)" until someone notices and deletes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)

CONFIG_FILENAME = "config.json"

DEFAULT_CONFIG = {
    "reserve_percent": 20,
    "no_reserve_hours": {"start": "02:00", "end": "07:00"},
    "smart_mode": {"enabled": True, "tasks_per_cycle": 3},
    "mode": "auto",
    "human_in_loop": {"ask_cadence": "every_window_reset"},
    "telegram_verbosity": "narrow",
    "projects": [],
}
# Keys whose defaults get merged one level deep, so a partial override (e.g.
# just human_in_loop.ask_cadence) doesn't silently drop sibling defaults.
# An explicit `null` for one of these (e.g. no_reserve_hours) is a real,
# meaningful value (disables the window) and must NOT be re-merged with the
# default — the isinstance(..., dict) check below is what preserves that.
_DEEP_MERGE_KEYS = ("no_reserve_hours", "smart_mode", "human_in_loop")


def squeezer_home() -> Path:
    return Path(os.environ.get("SQUEEZER_HOME", "~/.config/squeezer")).expanduser()


def config_path() -> Path:
    return squeezer_home() / CONFIG_FILENAME


def load_config() -> dict:
    path = config_path()
    data = {}
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
        except json.JSONDecodeError:
            data = {}  # corrupt/truncated (e.g. write interrupted mid-flight) — fall back to defaults

    merged = {**DEFAULT_CONFIG, **data}
    for key in _DEEP_MERGE_KEYS:
        if key in data and isinstance(data[key], dict):
            merged[key] = {**DEFAULT_CONFIG[key], **data[key]}
    return merged


def save_config(cfg: dict) -> None:
    atomic_write_text(config_path(), json.dumps(cfg, indent=2) + "\n")


def set_mode(new_mode: str) -> None:
    if new_mode not in ("auto", "human_in_loop"):
        raise ValueError(f"unknown mode: {new_mode!r} (expected 'auto' or 'human_in_loop')")
    cfg = load_config()
    cfg["mode"] = new_mode
    save_config(cfg)


def projects() -> list[dict]:
    return load_config().get("projects", [])


def state_dir() -> Path:
    d = squeezer_home() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def todos_dir() -> Path:
    d = squeezer_home() / "todos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_env() -> dict:
    """.env-style secrets (Telegram token/ids) from SQUEEZER_HOME/.env.
    Process environment always wins over the file, matching a normal
    dotenv-loader convention."""
    env = {}
    env_path = squeezer_home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    env.update(os.environ)
    return env
