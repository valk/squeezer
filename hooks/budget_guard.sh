#!/usr/bin/env bash
# PreToolUse hook: blocks tool calls once the configured token reserve is
# breached for the current window — but only for squeezer's own daemon-
# spawned turns (cwd == SQUEEZER_HOME, see daemon/daemon.py's spawn_claude).
# This hook is chained globally into every Claude Code session, but the
# reserve exists to protect a human's own quota, not gate it, so
# usage_lib.py's cmd_check always allows any other session (interactive, or
# an agent working on some other project). Reads real usage from the
# session's own transcript (not a heuristic) via daemon/usage_lib.py — which
# itself reads and writes SQUEEZER_HOME (the per-install control directory),
# never this hook's own location, so this resolves correctly whether
# squeezer is installed as a plugin (CLAUDE_PLUGIN_ROOT set by Claude Code)
# or run directly from a checkout of this repo for squeezer's own
# development (falls back to this file's own repo layout).
set -euo pipefail
DIR="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
exec python3 "$DIR/daemon/usage_lib.py" check
