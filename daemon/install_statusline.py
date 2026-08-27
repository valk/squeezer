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

`remove_global` is the inverse, invoked by `/squeezer:uninstall`: it strips
just squeezer's own line back out, leaving any other chained command
untouched. Claude Code's own `/plugin uninstall` has no way to know squeezer
made this edit, so nothing removes it unless `/squeezer:uninstall` does.

--- stdin-sharing across the chain ---

Claude Code pipes one JSON payload (model, cost, rate_limits, ...) to the
statusLine command per render. When that command is multiple chained lines
run as one shell script, only the *first* line to read stdin actually gets
it — verified empirically: of two sequential `cat`s in one `bash -c`, the
second gets 0 bytes, because a pipe can't be read twice. hud_status.py wants
that payload too (see its _real_five_hour_percent_from_stdin), so a 2+ line
composed command captures the payload once into a shell variable and pipes
an independent copy to every line via `_wrap_command` — existing commands
(e.g. claude-hud's own) get this transparently; they still receive the same
bytes they always did, just relayed through the variable instead of
inherited directly. A single-command chain (squeezer alone, the common
fresh-install case) skips the wrapper entirely since there's no sharing to
do, keeping that case's command exactly what it was before this existed.
`_unwrap_command` is the inverse, used to recover the underlying bare
command lines from a previously-installed (possibly already-wrapped)
command before re-filtering/re-appending squeezer's own line — it also
tolerates a pre-wrapping-era command (plain, un-prefixed lines) unchanged,
so upgrading from an older squeezer install works the same as any other
chain.
"""
import json
import sys
from pathlib import Path

MARKER = "daemon/hud_status.py"

_STDIN_VAR = "_SQUEEZER_STATUSLINE_STDIN"
_PREAMBLE = f'{_STDIN_VAR}="$(cat)"'
_PIPE_PREFIX = f"printf '%s' \"${_STDIN_VAR}\" | "


def squeezer_command(plugin_root: str) -> str:
    return f"python3 {plugin_root}/{MARKER}"


def _unwrap_command(existing_command: str | None) -> list[str]:
    """Recovers the bare (unwrapped) command lines from a possibly-wrapped
    existing statusLine command — see the module docstring's stdin-sharing
    section. Lines without the pipe prefix (a pre-wrapping-era command, or
    one where wrapping was skipped because it was a single line) pass
    through unchanged, so this is safe to call on any existing command."""
    lines = [line for line in (existing_command or "").split("\n") if line.strip()]
    if lines and lines[0] == _PREAMBLE:
        lines = lines[1:]
    return [
        line[len(_PIPE_PREFIX):] if line.startswith(_PIPE_PREFIX) else line
        for line in lines
    ]


def _wrap_command(bare_lines: list[str]) -> str:
    """Inverse of _unwrap_command. Only wraps when there are 2+ lines to
    share stdin between — a single command needs no wrapping since it
    already inherits the full payload directly."""
    if len(bare_lines) <= 1:
        return "\n".join(bare_lines)
    return "\n".join([_PREAMBLE, *(f"{_PIPE_PREFIX}{line}" for line in bare_lines)])


def compose_command(existing_command: str | None, plugin_root: str) -> str:
    """Keep every existing line except a prior squeezer line (dropped so a
    plugin_root refresh replaces it instead of piling up), then append the
    current squeezer line last so it always renders as the bottom row."""
    lines = [line for line in _unwrap_command(existing_command) if MARKER not in line]
    lines.append(squeezer_command(plugin_root))
    return _wrap_command(lines)


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


def remove_global(settings_path: Path | None = None) -> Path:
    """Reverses install_global: drops squeezer's own line from the global
    statusLine.command, leaving any other chained command (e.g. claude-hud)
    in place. Removes the statusLine key entirely if squeezer's line was the
    only one there. No-op if the file or the line doesn't exist — `/plugin
    uninstall` has no counterpart for this, so `/squeezer:uninstall` is the
    only thing that calls it."""
    settings_path = settings_path or global_settings_path()
    if not settings_path.exists():
        return settings_path
    settings = json.loads(settings_path.read_text())
    command = settings.get("statusLine", {}).get("command", "")
    if MARKER not in command:
        return settings_path
    lines = [line for line in _unwrap_command(command) if MARKER not in line]
    if lines:
        settings["statusLine"]["command"] = _wrap_command(lines)
    else:
        del settings["statusLine"]
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
    if len(sys.argv) == 2 and sys.argv[1] == "uninstall":
        result_path = remove_global()
        print(f"statusLine line removed (global): {result_path}")
    elif len(sys.argv) == 3:
        result_path = install(Path(sys.argv[1]), sys.argv[2])
        print(f"statusLine installed (global, chained below any existing statusLine): {result_path}")
    else:
        print("usage: install_statusline.py <squeezer_home> <plugin_root>  |  install_statusline.py uninstall", file=sys.stderr)
        sys.exit(1)
