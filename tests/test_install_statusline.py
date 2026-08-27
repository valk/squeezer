"""Tests for daemon/install_statusline.py — chaining squeezer's HUD line
onto the global ~/.claude/settings.json statusLine, invoked by
`/squeezer:setup`."""
import importlib.util
import json
from pathlib import Path

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "install_statusline", SQUEEZER_DIR / "daemon" / "install_statusline.py"
)
install_statusline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_statusline)


def test_install_global_creates_settings_with_statusline(tmp_path):
    settings_path = tmp_path / "settings.json"
    result = install_statusline.install_global("/plugin/root", settings_path)
    assert result == settings_path
    settings = json.loads(settings_path.read_text())
    assert settings["statusLine"]["type"] == "command"
    assert settings["statusLine"]["command"] == "python3 /plugin/root/daemon/hud_status.py"
    assert settings["statusLine"]["refreshInterval"] == 5


def test_install_global_chains_onto_existing_statusline(tmp_path):
    """Two chained lines get wrapped so each gets its own full copy of
    Claude Code's piped stdin (see install_statusline.py's stdin-sharing
    section) — claude-hud's own line included, even though it doesn't know
    anything changed."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "bun run claude-hud"},
    }))

    install_statusline.install_global("/plugin/root", settings_path)

    settings = json.loads(settings_path.read_text())
    lines = settings["statusLine"]["command"].split("\n")
    assert lines == [
        '_SQUEEZER_STATUSLINE_STDIN="$(cat)"',
        'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | bun run claude-hud',
        'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | python3 /plugin/root/daemon/hud_status.py',
    ]


def test_install_global_preserves_existing_unrelated_settings(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"model": "sonnet"}))

    install_statusline.install_global("/plugin/root", settings_path)

    settings = json.loads(settings_path.read_text())
    assert settings["model"] == "sonnet"
    assert "statusLine" in settings


def test_install_global_is_idempotent_and_refreshes_plugin_root(tmp_path):
    """Reinstalling must unwrap the already-wrapped command from the first
    install, replace just squeezer's own line, and re-wrap — not double-wrap
    or leave a stale plugin_root behind."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "bun run claude-hud"},
    }))

    install_statusline.install_global("/old/root", settings_path)
    install_statusline.install_global("/new/root", settings_path)

    settings = json.loads(settings_path.read_text())
    lines = settings["statusLine"]["command"].split("\n")
    assert lines == [
        '_SQUEEZER_STATUSLINE_STDIN="$(cat)"',
        'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | bun run claude-hud',
        'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | python3 /new/root/daemon/hud_status.py',
    ]


def test_install_global_reinstall_from_pre_wrapping_era_command(tmp_path):
    """A settings.json written by an older squeezer (plain lines, no stdin
    wrapper at all) must upgrade cleanly to the wrapped form rather than
    treating the whole unwrapped blob as one bare command."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {
            "type": "command",
            "command": "bun run claude-hud\npython3 /old/root/daemon/hud_status.py",
        },
    }))

    install_statusline.install_global("/new/root", settings_path)

    settings = json.loads(settings_path.read_text())
    lines = settings["statusLine"]["command"].split("\n")
    assert lines == [
        '_SQUEEZER_STATUSLINE_STDIN="$(cat)"',
        'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | bun run claude-hud',
        'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | python3 /new/root/daemon/hud_status.py',
    ]


def test_remove_global_strips_squeezer_line_leaving_others(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {
            "type": "command",
            "command": "bun run claude-hud\npython3 /plugin/root/daemon/hud_status.py",
        },
        "model": "sonnet",
    }))

    install_statusline.remove_global(settings_path)

    settings = json.loads(settings_path.read_text())
    assert settings["statusLine"]["command"] == "bun run claude-hud"
    assert settings["model"] == "sonnet"


def test_remove_global_drops_statusline_key_when_squeezer_was_only_line(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "python3 /plugin/root/daemon/hud_status.py"},
        "model": "sonnet",
    }))

    install_statusline.remove_global(settings_path)

    settings = json.loads(settings_path.read_text())
    assert "statusLine" not in settings
    assert settings["model"] == "sonnet"


def test_remove_global_collapses_wrapped_command_to_bare_single_line(tmp_path):
    """Once squeezer's own line is stripped from a wrapped 2-line command,
    only one line remains — no more stdin-sharing to do, so the result
    should be the plain bare command, not still wrapped in a
    now-pointless single-line capture-and-replay."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {
            "type": "command",
            "command": (
                '_SQUEEZER_STATUSLINE_STDIN="$(cat)"\n'
                'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | bun run claude-hud\n'
                'printf \'%s\' "$_SQUEEZER_STATUSLINE_STDIN" | python3 /plugin/root/daemon/hud_status.py'
            ),
        },
    }))

    install_statusline.remove_global(settings_path)

    settings = json.loads(settings_path.read_text())
    assert settings["statusLine"]["command"] == "bun run claude-hud"


def test_remove_global_noop_when_no_squeezer_line(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "bun run claude-hud"},
    }))

    install_statusline.remove_global(settings_path)

    settings = json.loads(settings_path.read_text())
    assert settings["statusLine"]["command"] == "bun run claude-hud"


def test_remove_global_noop_when_file_missing(tmp_path):
    settings_path = tmp_path / "settings.json"
    result = install_statusline.remove_global(settings_path)
    assert result == settings_path
    assert not settings_path.exists()


def test_clear_stale_local_override_removes_old_squeezer_only_line(tmp_path):
    squeezer_home = tmp_path / "squeezer_home"
    local_path = squeezer_home / ".claude" / "settings.json"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "python3 /old/root/daemon/hud_status.py"},
        "model": "sonnet",
    }))

    install_statusline.clear_stale_local_override(squeezer_home)

    settings = json.loads(local_path.read_text())
    assert "statusLine" not in settings
    assert settings["model"] == "sonnet"


def test_clear_stale_local_override_leaves_unrelated_statusline_alone(tmp_path):
    squeezer_home = tmp_path / "squeezer_home"
    local_path = squeezer_home / ".claude" / "settings.json"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "bun run claude-hud"},
    }))

    install_statusline.clear_stale_local_override(squeezer_home)

    settings = json.loads(local_path.read_text())
    assert settings["statusLine"]["command"] == "bun run claude-hud"


def test_clear_stale_local_override_noop_when_file_missing(tmp_path):
    install_statusline.clear_stale_local_override(tmp_path / "squeezer_home")  # no raise


def test_install_clears_local_override_and_installs_global(tmp_path):
    squeezer_home = tmp_path / "squeezer_home"
    local_path = squeezer_home / ".claude" / "settings.json"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "python3 /old/root/daemon/hud_status.py"},
    }))
    global_path = tmp_path / "global_settings.json"

    result = install_statusline.install(squeezer_home, "/plugin/root", global_path)

    assert result == global_path
    assert "statusLine" not in json.loads(local_path.read_text())
    global_settings = json.loads(global_path.read_text())
    assert global_settings["statusLine"]["command"] == "python3 /plugin/root/daemon/hud_status.py"
