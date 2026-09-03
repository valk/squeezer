"""Tests for daemon/config.py — resolving SQUEEZER_HOME (the per-install
control directory, separate from wherever the plugin package itself is
installed) and loading/saving its config.json."""
import importlib.util
import json
from pathlib import Path

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("config", SQUEEZER_DIR / "daemon" / "config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)


def test_squeezer_home_defaults_to_dot_config(monkeypatch):
    monkeypatch.delenv("SQUEEZER_HOME", raising=False)
    assert config.squeezer_home() == Path.home() / ".config" / "squeezer"


def test_squeezer_home_respects_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    assert config.squeezer_home() == tmp_path


def test_load_config_returns_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    cfg = config.load_config()
    assert cfg["mode"] == "auto"
    assert cfg["reserve_percent"] == 20
    assert cfg["no_reserve_hours"] == {"start": "02:00", "end": "07:00"}
    assert cfg["smart_mode"] == {"enabled": True, "tasks_per_cycle": 3}
    assert cfg["human_in_loop"] == {"ask_cadence": "every_window_reset"}
    assert cfg["telegram_verbosity"] == "narrow"
    assert cfg["projects"] == []


def test_load_config_partial_override_keeps_sibling_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(json.dumps({"human_in_loop": {"ask_cadence": "daily"}}))
    cfg = config.load_config()
    assert cfg["human_in_loop"]["ask_cadence"] == "daily"
    assert cfg["reserve_percent"] == 20  # untouched sibling key still defaulted


def test_load_config_falls_back_to_defaults_on_corrupt_file(tmp_path, monkeypatch):
    # e.g. a process killed mid-write, leaving a 0-byte file behind.
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text("")
    cfg = config.load_config()
    assert cfg["mode"] == "auto"
    assert cfg["reserve_percent"] == 20


def test_atomic_write_text_leaves_no_partial_file_on_disk(tmp_path):
    path = tmp_path / "nested" / "state.json"
    config.atomic_write_text(path, '{"a": 1}')
    assert path.read_text() == '{"a": 1}'
    assert list(tmp_path.rglob("*.tmp")) == []


def test_load_config_explicit_null_no_reserve_hours_disables_window(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(json.dumps({"no_reserve_hours": None}))
    cfg = config.load_config()
    assert cfg["no_reserve_hours"] is None


def test_save_config_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    cfg = config.load_config()
    cfg["mode"] = "human_in_loop"
    config.save_config(cfg)
    assert config.load_config()["mode"] == "human_in_loop"


def test_set_mode_updates_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    config.set_mode("human_in_loop")
    assert config.load_config()["mode"] == "human_in_loop"


def test_set_mode_rejects_unknown_value(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        config.set_mode("sleepwalking")


def test_projects_helper_returns_registered_list(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(json.dumps({
        "projects": [{"name": "acme", "path": "/tmp/acme"}],
    }))
    assert config.projects() == [{"name": "acme", "path": "/tmp/acme"}]


def test_state_dir_and_todos_dir_are_created(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    state_dir = config.state_dir()
    todos_dir = config.todos_dir()
    assert state_dir == tmp_path / "state"
    assert state_dir.is_dir()
    assert todos_dir == tmp_path / "todos"
    assert todos_dir.is_dir()


def test_load_env_reads_dot_env_from_squeezer_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=abc123\n")
    env = config.load_env()
    assert env["TELEGRAM_BOT_TOKEN"] == "abc123"


def test_load_env_process_environment_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=from-file\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-process-env")
    env = config.load_env()
    assert env["TELEGRAM_BOT_TOKEN"] == "from-process-env"
