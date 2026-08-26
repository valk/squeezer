#!/usr/bin/env python3
"""Wires squeezer's HUD statusLine (mode/budget, TODO counts, latest worklog
insight — the same line `/squeezer:status` leads with and telegram_lib
prepends to messages) into the GLOBAL ~/.claude/settings.json, so it shows
in every Claude Code session, not just ones opened at SQUEEZER_HOME.

Rather than overwrite whatever statusLine command is already there (e.g. a
plugin like claude-hud), this chains onto it: the existing command runs
first, then squeezer's own line is appended after a newline. Claude Code's
statusLine renders one row per line of stdout, so the effect is squeezer's
HUD showing as an extra line below whatever was already configured.
Invoked by `/squeezer:setup`; idempotent — re-running it (e.g. after a
plugin upgrade changes plugin_root) replaces just squeezer's own line
in place rather than duplicating it or disturbing what's chained ahead of
it.

Older squeezer versions wired this into SQUEEZER_HOME's own *project*-scoped
settings.json instead, which Claude Code's precedence would rank above this
global file — that then shadows the global chained line (dropping whatever
was chained ahead of squeezer, e.g. claude-hud) whenever a session opens at
SQUEEZER_HOME. `clear_stale_local_override` removes that leftover so the
global line wins everywhere consistently.
"""
import json
import sys
from pathlib import Path

MARKER = "daemon/hud_status.py"


def squeezer_command(plugin_root: str) -> str:
    return f"python3 {plugin_root}/{MARKER}"


def compose_command(existing_command: str | None, plugin_root: str) -> str:
    """Keep every existing line except a prior squeezer line (dropped so a
    plugin_root refresh replaces it instead of piling up), then append the
    current squeezer line last so it always renders as the bottom row."""
    lines = [
        line for line in (existing_command or "").split("\n")
        if line.strip() and MARKER not in line
    ]
    lines.append(squeezer_command(plugin_root))
    return "\n".join(lines)


def global_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def install_global(plugin_root: str, settings_path: Path | None = None) -> Path:
    settings_path = settings_path or global_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    existing = settings.get("statusLine", {})
    block = {
        "type": "command",
        "command": compose_command(existing.get("command"), plugin_root),
        "refreshInterval": min(existing.get("refreshInterval", 5), 5),
    }
    if "padding" in existing:
        block["padding"] = existing["padding"]
    settings["statusLine"] = block
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings_path


def clear_stale_local_override(squeezer_home: Path) -> None:
    local_path = squeezer_home / ".claude" / "settings.json"
    if not local_path.exists():
        return
    settings = json.loads(local_path.read_text())
    if MARKER in settings.get("statusLine", {}).get("command", ""):
        del settings["statusLine"]
        local_path.write_text(json.dumps(settings, indent=2) + "\n")


def install(squeezer_home: Path, plugin_root: str, settings_path: Path | None = None) -> Path:
    clear_stale_local_override(squeezer_home)
    return install_global(plugin_root, settings_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: install_statusline.py <squeezer_home> <plugin_root>", file=sys.stderr)
        sys.exit(1)
    result_path = install(Path(sys.argv[1]), sys.argv[2])
    print(f"statusLine installed (global, chained below any existing statusLine): {result_path}")
