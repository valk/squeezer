"""Tests for daemon/self_heal_wiring.py — the SessionStart hook body that
re-wires squeezer's daemon service and chained statusLine line if either
went missing since `/squeezer:setup` last ran (e.g. a `/plugin uninstall`
+ `/plugin install` cycle, which doesn't re-run setup on its own)."""
import importlib.util
import json
from pathlib import Path

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "self_heal_wiring", SQUEEZER_DIR / "daemon" / "self_heal_wiring.py"
)
self_heal_wiring = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(self_heal_wiring)


def _mock_darwin(monkeypatch, tmp_path):
    plist_path = tmp_path / "LaunchAgents" / f"{self_heal_wiring.install_service.LABEL}.plist"
    monkeypatch.setattr(self_heal_wiring.install_service, "launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(self_heal_wiring.install_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(self_heal_wiring.install_service, "resolve_claude_dir", lambda: "/usr/local/bin")
    monkeypatch.setattr(self_heal_wiring.install_service.subprocess, "run", lambda *a, **k: None)
    return plist_path


def test_heal_noop_when_setup_never_ran(tmp_path, monkeypatch):
    squeezer_home = tmp_path / "squeezer_home"  # no config.json
    _mock_darwin(monkeypatch, tmp_path)
    settings_path = tmp_path / "settings.json"

    repaired = self_heal_wiring.heal(squeezer_home, "/plugin/root", settings_path)

    assert repaired == []
    assert not settings_path.exists()


def test_heal_noop_when_everything_already_wired(tmp_path, monkeypatch):
    squeezer_home = tmp_path / "squeezer_home"
    squeezer_home.mkdir()
    (squeezer_home / "config.json").write_text("{}")
    plist_path = _mock_darwin(monkeypatch, tmp_path)
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("already installed")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "python3 /plugin/root/daemon/hud_status.py"},
    }))

    calls = []
    monkeypatch.setattr(self_heal_wiring.install_statusline, "install_global", lambda *a, **k: calls.append(a))

    repaired = self_heal_wiring.heal(squeezer_home, "/plugin/root", settings_path)

    assert repaired == []
    assert calls == []
    assert plist_path.read_text() == "already installed"  # untouched, not reinstalled


def test_heal_reinstalls_daemon_service_when_missing(tmp_path, monkeypatch):
    squeezer_home = tmp_path / "squeezer_home"
    squeezer_home.mkdir()
    (squeezer_home / "config.json").write_text("{}")
    plist_path = _mock_darwin(monkeypatch, tmp_path)  # plist not written -> missing
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "python3 /plugin/root/daemon/hud_status.py"},
    }))

    repaired = self_heal_wiring.heal(squeezer_home, "/plugin/root", settings_path)

    assert repaired == ["daemon service"]
    assert plist_path.exists()


def test_heal_rewires_statusline_when_missing(tmp_path, monkeypatch):
    squeezer_home = tmp_path / "squeezer_home"
    squeezer_home.mkdir()
    (squeezer_home / "config.json").write_text("{}")
    plist_path = _mock_darwin(monkeypatch, tmp_path)
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("already installed")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "bun run claude-hud"},
    }))

    repaired = self_heal_wiring.heal(squeezer_home, "/plugin/root", settings_path)

    assert repaired == ["statusLine"]
    settings = json.loads(settings_path.read_text())
    lines = settings["statusLine"]["command"].split("\n")
    assert lines == [
        '_SQUEEZER_STATUSLINE_STDIN="$(cat)"',
        'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | bun run claude-hud',
        'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | python3 /plugin/root/daemon/hud_status.py',
    ]


def test_heal_repairs_both_when_both_missing(tmp_path, monkeypatch):
    squeezer_home = tmp_path / "squeezer_home"
    squeezer_home.mkdir()
    (squeezer_home / "config.json").write_text("{}")
    plist_path = _mock_darwin(monkeypatch, tmp_path)
    settings_path = tmp_path / "settings.json"  # doesn't exist -> statusline missing too

    repaired = self_heal_wiring.heal(squeezer_home, "/plugin/root", settings_path)

    assert repaired == ["daemon service", "statusLine"]
    assert plist_path.exists()
    assert settings_path.exists()


def test_statusline_missing_true_when_settings_file_absent(tmp_path):
    assert self_heal_wiring.statusline_missing(tmp_path / "nope.json") is True


def test_statusline_missing_false_when_marker_present(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {"command": "python3 /x/daemon/hud_status.py"},
    }))
    assert self_heal_wiring.statusline_missing(settings_path) is False


def test_main_never_raises_when_heal_raises(monkeypatch):
    """The realistic trigger is heal() hitting corrupt JSON in the global
    ~/.claude/settings.json (statusline_missing parses it) — exercised here
    directly against heal() to keep this test isolated from real
    filesystem/launchctl state."""
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(self_heal_wiring, "heal", _boom)

    self_heal_wiring.main()  # must not raise


def test_main_is_silent_when_nothing_repaired(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))  # no config.json -> heal() no-ops

    self_heal_wiring.main()

    assert capsys.readouterr().out == ""
