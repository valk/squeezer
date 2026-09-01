"""Tests for daemon/check_mcp_deps.py — the setup-time check that the `mcp`
Python package (needed by mcp/telegram_server.py's `from mcp.server.fastmcp
import FastMCP`) is actually importable by the interpreter squeezer's daemon
service will use, installing it if not. Real subprocess/network calls are
mocked out."""
import importlib.util
from pathlib import Path

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "check_mcp_deps", SQUEEZER_DIR / "daemon" / "check_mcp_deps.py"
)
check_mcp_deps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_mcp_deps)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_is_importable_true_on_zero_exit(monkeypatch):
    monkeypatch.setattr(check_mcp_deps.subprocess, "run", lambda cmd, **k: FakeCompleted(0))
    assert check_mcp_deps.is_importable("/usr/bin/python3") is True


def test_is_importable_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(check_mcp_deps.subprocess, "run", lambda cmd, **k: FakeCompleted(1))
    assert check_mcp_deps.is_importable("/usr/bin/python3") is False


def test_is_venv_interpreter_true_when_prefixes_differ(monkeypatch):
    monkeypatch.setattr(check_mcp_deps.subprocess, "run", lambda cmd, **k: FakeCompleted(0, stdout="True\n"))
    assert check_mcp_deps.is_venv_interpreter("/venv/bin/python3") is True


def test_is_venv_interpreter_false_for_system_python(monkeypatch):
    monkeypatch.setattr(check_mcp_deps.subprocess, "run", lambda cmd, **k: FakeCompleted(0, stdout="False\n"))
    assert check_mcp_deps.is_venv_interpreter("/usr/bin/python3") is False


def test_is_venv_interpreter_false_when_check_itself_fails(monkeypatch):
    monkeypatch.setattr(check_mcp_deps.subprocess, "run", lambda cmd, **k: FakeCompleted(1))
    assert check_mcp_deps.is_venv_interpreter("/usr/bin/python3") is False


def test_ensure_mcp_installed_skips_install_when_already_satisfied(monkeypatch):
    monkeypatch.setattr(check_mcp_deps, "is_importable", lambda python_bin: True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not attempt install when already importable")

    monkeypatch.setattr(check_mcp_deps, "ensure_pip", fail_if_called)
    monkeypatch.setattr(check_mcp_deps, "install_mcp", fail_if_called)

    result = check_mcp_deps.ensure_mcp_installed("/usr/bin/python3")
    assert result == {"ok": True, "already_satisfied": True, "error": None}


def test_ensure_mcp_installed_installs_when_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(check_mcp_deps, "is_importable", lambda python_bin: bool(calls))
    monkeypatch.setattr(check_mcp_deps, "ensure_pip", lambda python_bin: {"ok": True, "error": None})

    def fake_install(python_bin):
        calls.append(python_bin)
        return {"ok": True, "error": None}

    monkeypatch.setattr(check_mcp_deps, "install_mcp", fake_install)

    result = check_mcp_deps.ensure_mcp_installed("/usr/bin/python3")
    assert result == {"ok": True, "already_satisfied": False, "error": None}
    assert calls == ["/usr/bin/python3"]


def test_ensure_mcp_installed_reports_pip_bootstrap_failure(monkeypatch):
    monkeypatch.setattr(check_mcp_deps, "is_importable", lambda python_bin: False)
    monkeypatch.setattr(check_mcp_deps, "ensure_pip", lambda python_bin: {"ok": False, "error": "no network"})

    result = check_mcp_deps.ensure_mcp_installed("/usr/bin/python3")
    assert result["ok"] is False
    assert result["already_satisfied"] is False
    assert "no network" in result["error"]


def test_ensure_mcp_installed_reports_install_failure(monkeypatch):
    monkeypatch.setattr(check_mcp_deps, "is_importable", lambda python_bin: False)
    monkeypatch.setattr(check_mcp_deps, "ensure_pip", lambda python_bin: {"ok": True, "error": None})
    monkeypatch.setattr(check_mcp_deps, "install_mcp", lambda python_bin: {"ok": False, "error": "pip exploded"})

    result = check_mcp_deps.ensure_mcp_installed("/usr/bin/python3")
    assert result["ok"] is False
    assert "pip exploded" in result["error"]


def test_ensure_mcp_installed_reports_still_broken_after_install(monkeypatch):
    # install reports success but the import still fails afterward.
    monkeypatch.setattr(check_mcp_deps, "is_importable", lambda python_bin: False)
    monkeypatch.setattr(check_mcp_deps, "ensure_pip", lambda python_bin: {"ok": True, "error": None})
    monkeypatch.setattr(check_mcp_deps, "install_mcp", lambda python_bin: {"ok": True, "error": None})

    result = check_mcp_deps.ensure_mcp_installed("/usr/bin/python3")
    assert result["ok"] is False
    assert "still fails" in result["error"]


def test_install_mcp_plain_user_install_success(monkeypatch):
    monkeypatch.setattr(check_mcp_deps, "is_venv_interpreter", lambda python_bin: False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(0)

    monkeypatch.setattr(check_mcp_deps.subprocess, "run", fake_run)
    result = check_mcp_deps.install_mcp("/usr/bin/python3")
    assert result == {"ok": True, "error": None}
    assert calls == [["/usr/bin/python3", "-m", "pip", "install", "--user", check_mcp_deps.REQUIRED_SPEC]]


def test_install_mcp_omits_user_flag_inside_a_venv(monkeypatch):
    # pip refuses --user inside a venv ("User site-packages are not visible
    # in this virtualenv") — verified against a real `python3 -m venv`.
    monkeypatch.setattr(check_mcp_deps, "is_venv_interpreter", lambda python_bin: True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(0)

    monkeypatch.setattr(check_mcp_deps.subprocess, "run", fake_run)
    result = check_mcp_deps.install_mcp("/venv/bin/python3")
    assert result == {"ok": True, "error": None}
    assert calls == [["/venv/bin/python3", "-m", "pip", "install", check_mcp_deps.REQUIRED_SPEC]]
    assert "--user" not in calls[0]


def test_install_mcp_falls_back_on_externally_managed_environment(monkeypatch):
    monkeypatch.setattr(check_mcp_deps, "is_venv_interpreter", lambda python_bin: False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--break-system-packages" not in cmd:
            return FakeCompleted(1, stderr="error: externally-managed-environment")
        return FakeCompleted(0)

    monkeypatch.setattr(check_mcp_deps.subprocess, "run", fake_run)
    result = check_mcp_deps.install_mcp("/usr/bin/python3")
    assert result == {"ok": True, "error": None}
    assert len(calls) == 2
    assert "--break-system-packages" in calls[1]


def test_install_mcp_reports_other_pip_errors_without_fallback(monkeypatch):
    monkeypatch.setattr(check_mcp_deps, "is_venv_interpreter", lambda python_bin: False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(1, stderr="no matching distribution found")

    monkeypatch.setattr(check_mcp_deps.subprocess, "run", fake_run)
    result = check_mcp_deps.install_mcp("/usr/bin/python3")
    assert result["ok"] is False
    assert "no matching distribution" in result["error"]
    assert len(calls) == 1


def test_ensure_pip_returns_ok_when_pip_already_present(monkeypatch):
    monkeypatch.setattr(check_mcp_deps.subprocess, "run", lambda cmd, **k: FakeCompleted(0))
    result = check_mcp_deps.ensure_pip("/usr/bin/python3")
    assert result == {"ok": True, "error": None}


def test_ensure_pip_bootstraps_via_ensurepip_when_pip_missing(monkeypatch):
    monkeypatch.setattr(check_mcp_deps, "is_venv_interpreter", lambda python_bin: False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--version" in cmd:
            return FakeCompleted(1)
        if "ensurepip" in cmd:
            return FakeCompleted(0)
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr(check_mcp_deps.subprocess, "run", fake_run)
    result = check_mcp_deps.ensure_pip("/usr/bin/python3")
    assert result == {"ok": True, "error": None}
    assert any("ensurepip" in c for c in calls)


def test_ensure_pip_falls_back_to_get_pip_when_ensurepip_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(check_mcp_deps, "is_venv_interpreter", lambda python_bin: False)

    def fake_urlretrieve(url, path):
        Path(path).write_text("# fake get-pip\n")

    monkeypatch.setattr(check_mcp_deps.urllib.request, "urlretrieve", fake_urlretrieve)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--version" in cmd or "ensurepip" in cmd:
            return FakeCompleted(1)
        # the get-pip.py invocation
        return FakeCompleted(0)

    monkeypatch.setattr(check_mcp_deps.subprocess, "run", fake_run)
    result = check_mcp_deps.ensure_pip("/usr/bin/python3")
    assert result == {"ok": True, "error": None}
    assert any(len(c) >= 2 and c[0] == "/usr/bin/python3" and c[1] != "-m" for c in calls)


def test_ensure_pip_omits_user_flag_inside_a_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(check_mcp_deps, "is_venv_interpreter", lambda python_bin: True)

    def fake_urlretrieve(url, path):
        Path(path).write_text("# fake get-pip\n")

    monkeypatch.setattr(check_mcp_deps.urllib.request, "urlretrieve", fake_urlretrieve)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--version" in cmd or "ensurepip" in cmd:
            return FakeCompleted(1)
        return FakeCompleted(0)

    monkeypatch.setattr(check_mcp_deps.subprocess, "run", fake_run)
    result = check_mcp_deps.ensure_pip("/venv/bin/python3")
    assert result == {"ok": True, "error": None}
    get_pip_call = next(c for c in calls if "ensurepip" not in c and "--version" not in c)
    assert "--user" not in get_pip_call


def test_ensure_pip_get_pip_falls_back_to_break_system_packages(monkeypatch):
    monkeypatch.setattr(check_mcp_deps, "is_venv_interpreter", lambda python_bin: False)

    def fake_urlretrieve(url, path):
        Path(path).write_text("# fake get-pip\n")

    monkeypatch.setattr(check_mcp_deps.urllib.request, "urlretrieve", fake_urlretrieve)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--version" in cmd or "ensurepip" in cmd:
            return FakeCompleted(1)
        if "--break-system-packages" not in cmd:
            return FakeCompleted(1, stderr="error: externally-managed-environment")
        return FakeCompleted(0)

    monkeypatch.setattr(check_mcp_deps.subprocess, "run", fake_run)
    result = check_mcp_deps.ensure_pip("/usr/bin/python3")
    assert result == {"ok": True, "error": None}


def test_ensure_pip_reports_download_failure(monkeypatch):
    monkeypatch.setattr(check_mcp_deps, "is_venv_interpreter", lambda python_bin: False)

    def fake_run(cmd, **kwargs):
        return FakeCompleted(1)

    monkeypatch.setattr(check_mcp_deps.subprocess, "run", fake_run)

    def fake_urlretrieve(url, path):
        raise OSError("network unreachable")

    monkeypatch.setattr(check_mcp_deps.urllib.request, "urlretrieve", fake_urlretrieve)

    result = check_mcp_deps.ensure_pip("/usr/bin/python3")
    assert result["ok"] is False
    assert "network unreachable" in result["error"]
