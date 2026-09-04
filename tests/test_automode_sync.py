"""Tests for daemon/automode_sync.py's SQUEEZER_HOME protection rules
(squeezer_home_rules and sync()'s unconditional application of them) — the
fix that closes the gap where a spawned headless turn (which runs with
cwd=SQUEEZER_HOME, see daemon.py's spawn_claude) could otherwise grant
itself elevation, clear its own TOTP lockout, or read the raw TOTP_SECRET
with no auto-mode restriction at all. Only covers what that fix added —
the rest of this module (per-project baseline_rules/sync behavior) has no
pre-existing coverage and isn't retroactively tested here."""
import importlib.util
import json
from pathlib import Path

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("automode_sync", SQUEEZER_DIR / "daemon" / "automode_sync.py")
automode_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(automode_sync)


def test_squeezer_home_rules_returns_expected_seven_rules():
    rules = automode_sync.squeezer_home_rules("/fake/home")
    assert rules == [
        "Write(/fake/home/state/elevation.json) in /fake/home",
        "Edit(/fake/home/state/elevation.json) in /fake/home",
        "Write(/fake/home/state/totp.json) in /fake/home",
        "Edit(/fake/home/state/totp.json) in /fake/home",
        "Read(/fake/home/.env) in /fake/home",
        "Write(/fake/home/.env) in /fake/home",
        "Edit(/fake/home/.env) in /fake/home",
    ]


def _write_squeezer_config(path: Path, projects=None):
    path.write_text(json.dumps({"projects": projects or []}))


def test_sync_with_no_projects_still_adds_squeezer_home_rules(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("SQUEEZER_HOME", str(home))
    monkeypatch.setattr(automode_sync, "GLOBAL_SETTINGS", tmp_path / "settings.json")

    config_path = tmp_path / "config.json"
    _write_squeezer_config(config_path, projects=[])

    automode_sync.sync(config_path)

    settings = json.loads((tmp_path / "settings.json").read_text())
    hard_deny = settings["autoMode"]["hard_deny"]
    for rule in automode_sync.squeezer_home_rules(str(home)):
        assert rule in hard_deny


def test_sync_twice_does_not_duplicate_squeezer_home_rules(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SQUEEZER_HOME", str(home))
    monkeypatch.setattr(automode_sync, "GLOBAL_SETTINGS", tmp_path / "settings.json")

    config_path = tmp_path / "config.json"
    _write_squeezer_config(config_path, projects=[])

    automode_sync.sync(config_path)
    automode_sync.sync(config_path)

    settings = json.loads((tmp_path / "settings.json").read_text())
    hard_deny = settings["autoMode"]["hard_deny"]
    for rule in automode_sync.squeezer_home_rules(str(home)):
        assert hard_deny.count(rule) == 1


def test_sync_adds_squeezer_home_rules_alongside_project_rules(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SQUEEZER_HOME", str(home))
    monkeypatch.setattr(automode_sync, "GLOBAL_SETTINGS", tmp_path / "settings.json")

    config_path = tmp_path / "config.json"
    _write_squeezer_config(config_path, projects=[{"name": "proj", "path": "/fake/proj"}])

    automode_sync.sync(config_path)

    settings = json.loads((tmp_path / "settings.json").read_text())
    hard_deny = settings["autoMode"]["hard_deny"]
    # per-project baseline rules are untouched by the new unconditional call
    for rule in automode_sync.baseline_rules("/fake/proj"):
        assert rule in hard_deny
    # and the SQUEEZER_HOME rules are present too
    for rule in automode_sync.squeezer_home_rules(str(home)):
        assert rule in hard_deny
