"""Tests for daemon/qr.py — the /squeezer:2fa-setup QR-code enrollment
helper. Mirrors test_check_mcp_deps.py's mocked-subprocess pattern for the
install-if-missing logic. generate_qr_png is exercised for real against the
actual qrcode/PIL packages, skipped if they aren't installed in this
environment (they're an optional dependency, not a stdlib-only one like
totp.py's)."""
import importlib.util
from pathlib import Path

import pytest

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("qr", SQUEEZER_DIR / "daemon" / "qr.py")
qr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qr)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_is_importable_true_on_zero_exit(monkeypatch):
    monkeypatch.setattr(qr.subprocess, "run", lambda cmd, **k: FakeCompleted(0))
    assert qr.is_importable("/usr/bin/python3") is True


def test_is_importable_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(qr.subprocess, "run", lambda cmd, **k: FakeCompleted(1))
    assert qr.is_importable("/usr/bin/python3") is False


def test_is_venv_interpreter_true_when_prefixes_differ(monkeypatch):
    monkeypatch.setattr(qr.subprocess, "run", lambda cmd, **k: FakeCompleted(0, stdout="True\n"))
    assert qr.is_venv_interpreter("/venv/bin/python3") is True


def test_is_venv_interpreter_false_for_system_python(monkeypatch):
    monkeypatch.setattr(qr.subprocess, "run", lambda cmd, **k: FakeCompleted(0, stdout="False\n"))
    assert qr.is_venv_interpreter("/usr/bin/python3") is False


def test_ensure_qrcode_installed_skips_install_when_already_satisfied(monkeypatch):
    monkeypatch.setattr(qr, "is_importable", lambda python_bin: True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not attempt install when already importable")

    monkeypatch.setattr(qr, "install_qrcode", fail_if_called)

    result = qr.ensure_qrcode_installed("/usr/bin/python3")
    assert result == {"ok": True, "already_satisfied": True, "error": None}


def test_ensure_qrcode_installed_installs_when_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(qr, "is_importable", lambda python_bin: bool(calls))

    def fake_install(python_bin):
        calls.append(python_bin)
        return {"ok": True, "error": None}

    monkeypatch.setattr(qr, "install_qrcode", fake_install)

    result = qr.ensure_qrcode_installed("/usr/bin/python3")
    assert result == {"ok": True, "already_satisfied": False, "error": None}
    assert calls == ["/usr/bin/python3"]


def test_ensure_qrcode_installed_reports_install_failure(monkeypatch):
    monkeypatch.setattr(qr, "is_importable", lambda python_bin: False)
    monkeypatch.setattr(qr, "install_qrcode", lambda python_bin: {"ok": False, "error": "pip exploded"})

    result = qr.ensure_qrcode_installed("/usr/bin/python3")
    assert result["ok"] is False
    assert "pip exploded" in result["error"]


def test_ensure_qrcode_installed_reports_still_broken_after_install(monkeypatch):
    monkeypatch.setattr(qr, "is_importable", lambda python_bin: False)
    monkeypatch.setattr(qr, "install_qrcode", lambda python_bin: {"ok": True, "error": None})

    result = qr.ensure_qrcode_installed("/usr/bin/python3")
    assert result["ok"] is False
    assert "still fails" in result["error"]


def test_install_qrcode_plain_user_install_success(monkeypatch):
    monkeypatch.setattr(qr, "is_venv_interpreter", lambda python_bin: False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(0)

    monkeypatch.setattr(qr.subprocess, "run", fake_run)
    result = qr.install_qrcode("/usr/bin/python3")
    assert result == {"ok": True, "error": None}
    assert calls == [["/usr/bin/python3", "-m", "pip", "install", "--user", qr.REQUIRED_SPEC]]


def test_install_qrcode_omits_user_flag_inside_a_venv(monkeypatch):
    monkeypatch.setattr(qr, "is_venv_interpreter", lambda python_bin: True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(0)

    monkeypatch.setattr(qr.subprocess, "run", fake_run)
    result = qr.install_qrcode("/venv/bin/python3")
    assert result == {"ok": True, "error": None}
    assert calls == [["/venv/bin/python3", "-m", "pip", "install", qr.REQUIRED_SPEC]]
    assert "--user" not in calls[0]


def test_install_qrcode_falls_back_on_externally_managed_environment(monkeypatch):
    monkeypatch.setattr(qr, "is_venv_interpreter", lambda python_bin: False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--break-system-packages" not in cmd:
            return FakeCompleted(1, stderr="error: externally-managed-environment")
        return FakeCompleted(0)

    monkeypatch.setattr(qr.subprocess, "run", fake_run)
    result = qr.install_qrcode("/usr/bin/python3")
    assert result == {"ok": True, "error": None}
    assert len(calls) == 2
    assert "--break-system-packages" in calls[1]


def test_install_qrcode_reports_other_pip_errors_without_fallback(monkeypatch):
    monkeypatch.setattr(qr, "is_venv_interpreter", lambda python_bin: False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(1, stderr="no matching distribution found")

    monkeypatch.setattr(qr.subprocess, "run", fake_run)
    result = qr.install_qrcode("/usr/bin/python3")
    assert result["ok"] is False
    assert "no matching distribution" in result["error"]
    assert len(calls) == 1


def test_generate_qr_png_writes_a_valid_png(tmp_path):
    pytest.importorskip("qrcode")
    pytest.importorskip("PIL")

    out = tmp_path / "code.png"
    qr.generate_qr_png("otpauth://totp/squeezer:squeezer?secret=ABC&issuer=squeezer", str(out))

    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
