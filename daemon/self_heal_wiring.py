#!/usr/bin/env python3
"""SessionStart hook body: re-wires squeezer's daemon service and chained
statusLine line if either has gone missing since `/squeezer:setup` last ran
— the common case being a `/plugin uninstall` + `/plugin install` cycle,
since `/plugin install` only registers squeezer's commands/hooks and never
re-runs setup on its own (see commands/setup.md). Also restarts the daemon
service when it's stale: still running, but wired to a different daemon.py
than this session's own CLAUDE_PLUGIN_ROOT — the signature of a plugin
update/reinstall (e.g. a new marketplace cache version) landing without
anyone restarting the already-running service, which otherwise keeps
serving the old code indefinitely (it's a persistent OS process, not
something Claude Code's plugin system manages or restarts on its own).

No-ops entirely if SQUEEZER_HOME/config.json doesn't exist: first-time
setup needs interactive input (Telegram credentials, real project
registration) a hook can't provide, so `/squeezer:setup` stays the only way
to bootstrap from scratch. Also deliberately does NOT reassert either piece
of wiring when it's already present and current — only repairs what's
actually missing or stale — so a healthy, up-to-date install isn't touched
(no launchd unload/load restarting the daemon, no rewritten settings.json)
on every single session start.

Never raises past `main()`: a broken session-start hook must not block the
session it's trying to help.
"""
import json
import os
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_service  # noqa: E402
import install_statusline  # noqa: E402


def daemon_service_missing() -> bool:
    system = platform.system()
    if system == "Darwin":
        return not install_service.launchd_plist_path().exists()
    if system == "Linux":
        return not install_service.systemd_unit_path().exists()
    return False  # unsupported platform: install() would no-op anyway


def daemon_service_stale(plugin_root: str) -> bool:
    """True when a daemon service is installed but wired to a different
    daemon.py than this session's own CLAUDE_PLUGIN_ROOT. Returns False
    (never falsely triggers a restart) when the installed config can't be
    parsed at all — see install_service.installed_daemon_script()."""
    installed = install_service.installed_daemon_script()
    if installed is None:
        return False
    expected = Path(plugin_root).resolve() / "daemon" / "daemon.py"
    return installed.resolve() != expected


def statusline_missing(settings_path: Path) -> bool:
    if not settings_path.exists():
        return True
    settings = json.loads(settings_path.read_text())
    command = settings.get("statusLine", {}).get("command", "")
    return install_statusline.MARKER not in command


def heal(squeezer_home: Path, plugin_root: str, settings_path: Path | None = None) -> list[str]:
    """Repairs whichever of {daemon service, statusLine} is missing, and
    restarts the daemon service if it's stale (see daemon_service_stale).
    Returns what it repaired (empty if nothing needed it, including the
    setup-hasn't-run-yet case)."""
    if not (squeezer_home / "config.json").exists():
        return []

    repaired = []
    if daemon_service_missing():
        install_service.install(squeezer_home=squeezer_home)
        repaired.append("daemon service")
    elif daemon_service_stale(plugin_root):
        install_service.install(squeezer_home=squeezer_home)
        repaired.append("daemon service (restarted for a plugin update)")

    settings_path = settings_path or install_statusline.global_settings_path()
    if statusline_missing(settings_path):
        install_statusline.install_global(plugin_root, settings_path)
        repaired.append("statusLine")

    return repaired


def _squeezer_home_from_env() -> Path:
    return Path(os.environ.get("SQUEEZER_HOME") or Path.home() / ".config" / "squeezer")


def _plugin_root_from_env() -> str:
    return os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parent.parent)


def main() -> None:
    try:
        repaired = heal(_squeezer_home_from_env(), _plugin_root_from_env())
    except Exception:
        return  # self-healing must never break session startup
    if repaired:
        print(f"\U0001f34b squeezer: re-wired {' and '.join(repaired)} — missing or out of date since last session (plugin reinstall/update, or manual cleanup)")


if __name__ == "__main__":
    main()
