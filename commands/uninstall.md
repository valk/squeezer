---
description: Tears down squeezer's machine-level footprint — the background daemon service and the chained statusLine line — without touching SQUEEZER_HOME.
---

`/plugin uninstall` only removes squeezer from Claude Code's own plugin
list; it has no way to know squeezer also registered an OS service and
edited the global `~/.claude/settings.json` statusLine during
`/squeezer:setup`. Run this first (or after) to clean those up. Work
through these steps in order, using the Bash tool. Every step is
idempotent / no-op-if-already-clean, so this is safe to re-run.

1. Stop and remove the background daemon service: `python3 ${CLAUDE_PLUGIN_ROOT}/daemon/install_service.py uninstall`. This unloads the launchd job (macOS) or disables the systemd --user unit (Linux) and deletes its service file.
2. Remove squeezer's line from the global `~/.claude/settings.json` statusLine: `python3 ${CLAUDE_PLUGIN_ROOT}/daemon/install_statusline.py uninstall`. This only strips squeezer's own chained line — any other statusline command chained there (e.g. claude-hud) is left exactly as it was before `/squeezer:setup` ran.
3. **Do not delete `SQUEEZER_HOME`** (`${SQUEEZER_HOME:-$HOME/.config/squeezer}`) — it holds the user's registered projects, TODOs, worklog, and Telegram credentials. Report its path and tell the user it's theirs to remove by hand if they actually want that data gone; never delete or move it yourself.
4. Report a short summary back: confirmation the daemon service is stopped and removed, confirmation the statusLine line is gone (and that any other chained statusline command was preserved), and the SQUEEZER_HOME path reminder from step 3.
