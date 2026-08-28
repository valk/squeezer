#!/usr/bin/env python3
"""Cross-platform installer for squeezer's daemon as an always-on OS
service — launchd on macOS, systemd --user on Linux. Replaces
bin/install_launchd.sh + launchd/*.template: there's only one process to
supervise now (daemon.py), not a 3-window tmux session, so the service just
needs to keep that one process alive across crashes/reboots. Invoked by the
`/squeezer:setup` command, not run directly during normal operation.
"""
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.vkhey.squeezer.daemon"
DAEMON_SCRIPT = Path(__file__).resolve().parent / "daemon.py"

# launchd/systemd --user start services with a minimal PATH that doesn't
# include the interactive shell's PATH (nvm, ~/.local/bin, pyenv shims,
# etc.), so subprocesses daemon.py spawns can't find `claude`, and — deeper
# in that same process tree — a headless `claude -p` turn spawning the
# `squeezer-telegram` MCP server via bare `python3` can resolve to whatever
# python3 happens to be first on THIS minimal PATH (e.g. Homebrew's, which
# won't have the `mcp` package) instead of the interpreter squeezer actually
# runs on. Resolve both once at install time (when we still have the
# installer's real PATH) and bake their directories into the service's own
# PATH — first, so they take priority over any same-named fallback.
FALLBACK_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def resolve_claude_dir() -> str | None:
    """Directory of the `claude` executable as found on PATH — deliberately
    not `.resolve()`d, since `claude` is commonly a symlink into a versioned
    install dir that has no file named `claude` in it."""
    claude_path = shutil.which("claude")
    return str(Path(claude_path).parent) if claude_path else None


def build_service_path(python_bin: str, claude_dir: str | None) -> str:
    """PATH for the service's own environment: the daemon's Python
    interpreter dir (so any `python3` spawned under it, e.g. by an MCP
    server, matches squeezer's own interpreter — the one guaranteed to have
    squeezer's dependencies installed), then `claude`'s dir, then a generic
    fallback. Order-preserving de-dup, since both can land in the same dir."""
    dirs = [str(Path(python_bin).parent), claude_dir, *FALLBACK_PATH.split(":")]
    seen = set()
    unique = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            unique.append(d)
    return ":".join(unique)


def launchd_plist(python_bin: str, daemon_script: Path, log_path: Path, claude_dir: str | None = None) -> str:
    path_value = build_service_path(python_bin, claude_dir)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_bin}</string>
    <string>{daemon_script}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{path_value}</string>
  </dict>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log_path}</string>
  <key>StandardErrorPath</key>
  <string>{log_path}</string>
</dict>
</plist>
"""


def systemd_unit(python_bin: str, daemon_script: Path, claude_dir: str | None = None) -> str:
    path_value = build_service_path(python_bin, claude_dir)
    return f"""[Unit]
Description=squeezer endless-loop orchestrator daemon

[Service]
Environment=PATH={path_value}
ExecStart={python_bin} {daemon_script}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "squeezer-daemon.service"


def installed_daemon_script() -> Path | None:
    """Daemon script path currently baked into the installed service config
    (plist ProgramArguments / systemd ExecStart), or None if no service is
    installed or its config can't be parsed. Lets a caller detect a plugin
    update/reinstall that moved CLAUDE_PLUGIN_ROOT (e.g. a new marketplace
    cache version) out from under an already-running (now stale) service —
    see daemon/self_heal_wiring.py."""
    system = platform.system()
    if system == "Darwin":
        path = launchd_plist_path()
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                args = plistlib.load(f).get("ProgramArguments", [])
        except Exception:
            return None
        return Path(args[1]) if len(args) > 1 else None

    if system == "Linux":
        path = systemd_unit_path()
        if not path.exists():
            return None
        for line in path.read_text().splitlines():
            if line.startswith("ExecStart="):
                parts = line[len("ExecStart="):].split()
                return Path(parts[1]) if len(parts) > 1 else None
        return None

    return None


def install(python_bin: str = None, squeezer_home: Path = None) -> dict:
    """Writes and enables the appropriate service file for this OS. Returns
    {"ok": bool, "path": str} or {"ok": False, "error": str}."""
    python_bin = python_bin or sys.executable
    squeezer_home = squeezer_home or (Path.home() / ".config" / "squeezer")
    system = platform.system()
    claude_dir = resolve_claude_dir()
    warning = None if claude_dir else "could not find 'claude' on PATH at install time — the service's PATH won't include it, so spawned turns will fail until this is fixed and the service is reinstalled"

    if system == "Darwin":
        path = launchd_plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        log_path = squeezer_home / "state" / "daemon.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist(python_bin, DAEMON_SCRIPT, log_path, claude_dir))
        subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)
        subprocess.run(["launchctl", "load", str(path)], check=True, capture_output=True)
        result = {"ok": True, "path": str(path)}
        if warning:
            result["warning"] = warning
        return result

    if system == "Linux":
        path = systemd_unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit(python_bin, DAEMON_SCRIPT, claude_dir))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", path.stem + ".service"], check=True, capture_output=True)
        # restart (not just "enable --now") so a re-install always picks up
        # a changed unit file/daemon script even if the service was already
        # running — "--now" alone is a no-op start when already active.
        subprocess.run(["systemctl", "--user", "restart", path.stem + ".service"], check=True, capture_output=True)
        result = {"ok": True, "path": str(path)}
        if warning:
            result["warning"] = warning
        return result

    return {"ok": False, "error": f"unsupported platform: {system} (only macOS and Linux are supported)"}


def uninstall() -> dict:
    system = platform.system()

    if system == "Darwin":
        path = launchd_plist_path()
        if path.exists():
            subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)
            path.unlink()
        return {"ok": True}

    if system == "Linux":
        path = systemd_unit_path()
        subprocess.run(["systemctl", "--user", "disable", "--now", path.stem + ".service"], check=False, capture_output=True)
        if path.exists():
            path.unlink()
        return {"ok": True}

    return {"ok": False, "error": f"unsupported platform: {system} (only macOS and Linux are supported)"}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "install"
    if cmd == "install":
        result = install()
    elif cmd == "uninstall":
        result = uninstall()
    else:
        print("usage: install_service.py {install|uninstall}", file=sys.stderr)
        sys.exit(1)
    print(result)
    if not result.get("ok"):
        sys.exit(1)
