"""Tests for daemon/install_service.py — the launchd (macOS) / systemd --user
(Linux) service generator that replaces bin/install_launchd.sh +
launchd/*.template. Only one process to supervise now (the daemon), not a
3-window tmux session. Real launchctl/systemctl calls are mocked out; only
the generation + file-writing logic is exercised."""
import importlib.util
from pathlib import Path

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "install_service", SQUEEZER_DIR / "daemon" / "install_service.py"
)
install_service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_service)


def test_launchd_plist_contains_label_and_paths():
    plist = install_service.launchd_plist("/usr/bin/python3", Path("/opt/squeezer/daemon.py"), Path("/tmp/daemon.log"))
    assert install_service.LABEL in plist
    assert "/usr/bin/python3" in plist
    assert "/opt/squeezer/daemon.py" in plist
    assert "/tmp/daemon.log" in plist
    assert "<key>RunAtLoad</key>" in plist


def test_launchd_plist_keeps_alive():
    plist = install_service.launchd_plist("/usr/bin/python3", Path("/opt/daemon.py"), Path("/tmp/x.log"))
    assert "<key>KeepAlive</key>" in plist


def test_launchd_plist_puts_python_and_claude_dirs_in_path():
    plist = install_service.launchd_plist(
        "/opt/py/bin/python3", Path("/opt/daemon.py"), Path("/tmp/x.log"), claude_dir="/Users/x/.local/bin"
    )
    assert "<key>PATH</key>" in plist
    assert "<string>/opt/py/bin:/Users/x/.local/bin:" in plist


def test_launchd_plist_falls_back_without_claude_dir():
    plist = install_service.launchd_plist("/opt/py/bin/python3", Path("/opt/daemon.py"), Path("/tmp/x.log"))
    assert install_service.FALLBACK_PATH in plist


def test_systemd_unit_contains_exec_and_restart():
    unit = install_service.systemd_unit("/usr/bin/python3", Path("/opt/squeezer/daemon.py"))
    assert "ExecStart=/usr/bin/python3 /opt/squeezer/daemon.py" in unit
    assert "Restart=always" in unit


def test_systemd_unit_puts_python_and_claude_dirs_in_path():
    unit = install_service.systemd_unit("/opt/py/bin/python3", Path("/opt/squeezer/daemon.py"), claude_dir="/opt/claude/bin")
    assert f"Environment=PATH=/opt/py/bin:/opt/claude/bin:{install_service.FALLBACK_PATH}" in unit


def test_build_service_path_puts_python_dir_first():
    path = install_service.build_service_path("/opt/py/bin/python3", "/opt/claude/bin")
    assert path.startswith("/opt/py/bin:/opt/claude/bin:")


def test_build_service_path_dedupes_when_same_dir():
    path = install_service.build_service_path("/opt/bin/python3", "/opt/bin")
    assert path.split(":").count("/opt/bin") == 1


def test_build_service_path_without_claude_dir():
    path = install_service.build_service_path("/opt/py/bin/python3", None)
    assert path == f"/opt/py/bin:{install_service.FALLBACK_PATH}"


def test_resolve_claude_dir_uses_shutil_which(monkeypatch):
    monkeypatch.setattr(install_service.shutil, "which", lambda name: "/Users/x/.local/bin/claude")
    assert install_service.resolve_claude_dir() == "/Users/x/.local/bin"


def test_resolve_claude_dir_none_when_not_found(monkeypatch):
    monkeypatch.setattr(install_service.shutil, "which", lambda name: None)
    assert install_service.resolve_claude_dir() is None


def test_resolve_claude_dir_does_not_follow_symlink(tmp_path, monkeypatch):
    """`claude` is commonly a symlink (e.g. ~/.local/bin/claude ->
    ~/.local/share/claude/versions/2.1.246, a versioned file with no
    sibling named `claude`) — the PATH entry must be the symlink's own
    directory, not its resolved target's directory."""
    real_dir = tmp_path / "versions"
    real_dir.mkdir()
    real_bin = real_dir / "2.1.246"
    real_bin.write_text("#!/bin/sh\n")
    real_bin.chmod(0o755)

    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "claude").symlink_to(real_bin)

    monkeypatch.setattr(install_service.shutil, "which", lambda name: str(link_dir / "claude"))
    assert install_service.resolve_claude_dir() == str(link_dir)


def test_install_unsupported_platform(monkeypatch):
    monkeypatch.setattr(install_service.platform, "system", lambda: "Windows")
    result = install_service.install()
    assert result["ok"] is False
    assert "unsupported" in result["error"].lower()


def test_install_macos_writes_plist_and_loads(tmp_path, monkeypatch):
    plist_path = tmp_path / "LaunchAgents" / f"{install_service.LABEL}.plist"
    monkeypatch.setattr(install_service, "launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(install_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install_service, "resolve_claude_dir", lambda: "/Users/x/.local/bin")

    calls = []
    monkeypatch.setattr(install_service.subprocess, "run", lambda cmd, **k: calls.append(cmd))

    result = install_service.install(python_bin="/usr/bin/python3", squeezer_home=tmp_path)

    assert result["ok"] is True
    assert "warning" not in result
    assert plist_path.exists()
    text = plist_path.read_text()
    assert "/usr/bin/python3" in text
    assert "/Users/x/.local/bin:" in text
    assert any(c[:2] == ["launchctl", "load"] for c in calls)


def test_install_macos_warns_when_claude_not_found(tmp_path, monkeypatch):
    plist_path = tmp_path / "LaunchAgents" / f"{install_service.LABEL}.plist"
    monkeypatch.setattr(install_service, "launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(install_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install_service, "resolve_claude_dir", lambda: None)
    monkeypatch.setattr(install_service.subprocess, "run", lambda cmd, **k: None)

    result = install_service.install(python_bin="/usr/bin/python3", squeezer_home=tmp_path)

    assert result["ok"] is True
    assert "warning" in result


def test_install_linux_writes_unit_and_enables(tmp_path, monkeypatch):
    unit_path = tmp_path / "systemd" / "user" / "squeezer-daemon.service"
    monkeypatch.setattr(install_service, "systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(install_service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install_service, "resolve_claude_dir", lambda: "/opt/claude/bin")

    calls = []
    monkeypatch.setattr(install_service.subprocess, "run", lambda cmd, **k: calls.append(cmd))

    result = install_service.install(python_bin="/usr/bin/python3", squeezer_home=tmp_path)

    assert result["ok"] is True
    assert unit_path.exists()
    assert "/opt/claude/bin:" in unit_path.read_text()
    assert any("enable" in c for c in calls)


def test_installed_daemon_script_darwin_reads_plist(tmp_path, monkeypatch):
    plist_path = tmp_path / f"{install_service.LABEL}.plist"
    plist_path.write_text(install_service.launchd_plist("/usr/bin/python3", Path("/opt/squeezer/daemon.py"), tmp_path / "x.log"))
    monkeypatch.setattr(install_service, "launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(install_service.platform, "system", lambda: "Darwin")

    assert install_service.installed_daemon_script() == Path("/opt/squeezer/daemon.py")


def test_installed_daemon_script_darwin_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(install_service, "launchd_plist_path", lambda: tmp_path / "nope.plist")
    monkeypatch.setattr(install_service.platform, "system", lambda: "Darwin")

    assert install_service.installed_daemon_script() is None


def test_installed_daemon_script_darwin_none_when_unparseable(tmp_path, monkeypatch):
    plist_path = tmp_path / f"{install_service.LABEL}.plist"
    plist_path.write_text("not a plist")
    monkeypatch.setattr(install_service, "launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(install_service.platform, "system", lambda: "Darwin")

    assert install_service.installed_daemon_script() is None


def test_installed_daemon_script_linux_reads_unit(tmp_path, monkeypatch):
    unit_path = tmp_path / "squeezer-daemon.service"
    unit_path.write_text(install_service.systemd_unit("/usr/bin/python3", Path("/opt/squeezer/daemon.py")))
    monkeypatch.setattr(install_service, "systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(install_service.platform, "system", lambda: "Linux")

    assert install_service.installed_daemon_script() == Path("/opt/squeezer/daemon.py")


def test_installed_daemon_script_linux_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(install_service, "systemd_unit_path", lambda: tmp_path / "nope.service")
    monkeypatch.setattr(install_service.platform, "system", lambda: "Linux")

    assert install_service.installed_daemon_script() is None


def test_install_linux_restarts_even_when_already_active(tmp_path, monkeypatch):
    unit_path = tmp_path / "systemd" / "user" / "squeezer-daemon.service"
    monkeypatch.setattr(install_service, "systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(install_service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install_service, "resolve_claude_dir", lambda: "/opt/claude/bin")

    calls = []
    monkeypatch.setattr(install_service.subprocess, "run", lambda cmd, **k: calls.append(cmd))

    install_service.install(python_bin="/usr/bin/python3", squeezer_home=tmp_path)

    assert any(c[:3] == ["systemctl", "--user", "restart"] for c in calls)


def test_uninstall_macos_removes_plist(tmp_path, monkeypatch):
    plist_path = tmp_path / f"{install_service.LABEL}.plist"
    plist_path.write_text("dummy")
    monkeypatch.setattr(install_service, "launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(install_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install_service.subprocess, "run", lambda *a, **k: None)

    result = install_service.uninstall()
    assert result["ok"] is True
    assert not plist_path.exists()


def test_uninstall_unsupported_platform(monkeypatch):
    monkeypatch.setattr(install_service.platform, "system", lambda: "Windows")
    result = install_service.uninstall()
    assert result["ok"] is False
