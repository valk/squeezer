"""Tests for daemon/worklog_query.py — asking the worklog why a past
decision was made. Loads the module via importlib against a scratch
SQUEEZER_HOME, same pattern as tests/test_config.py."""
import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parent.parent / "daemon" / "worklog_query.py"


@pytest.fixture
def wq(tmp_path, monkeypatch):
    """The module, freshly imported against a scratch SQUEEZER_HOME."""
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("worklog_query", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_worklog(tmp_path, text):
    worklog = tmp_path / "state" / "worklog.md"
    worklog.parent.mkdir(parents=True, exist_ok=True)
    worklog.write_text(text)
    return worklog


def test_read_worklog_returns_file_contents(wq, tmp_path):
    _write_worklog(tmp_path, "# Worklog\n\n## 2026-08-27\n\n- Chose X over Y.\n")
    assert "Chose X over Y." in wq.read_worklog()


def test_read_worklog_returns_none_when_missing(wq):
    assert wq.read_worklog() is None


def test_read_worklog_returns_none_when_empty(wq, tmp_path):
    _write_worklog(tmp_path, "   \n")
    assert wq.read_worklog() is None


def test_build_prompt_contains_question_and_worklog(wq):
    prompt = wq.build_prompt("why did we pick acme?", "## 2026-08-27\n\n- Picked acme.")
    assert "why did we pick acme?" in prompt
    assert "Picked acme." in prompt


def test_build_prompt_keeps_the_tail_when_oversized(wq):
    """Truncation must drop the OLDEST history, not the newest — recent
    entries are the ones most likely to hold the answer, and getting this
    backwards would silently discard them."""
    oldest = "## 2020-01-01\n\n- Ancient decision nobody asks about.\n"
    newest = "## 2026-09-05\n\n- The decision actually being asked about.\n"
    filler = "x" * (wq.MAX_WORKLOG_CHARS + 1000)
    prompt = wq.build_prompt("why?", oldest + filler + newest)
    assert "The decision actually being asked about." in prompt
    assert "Ancient decision nobody asks about." not in prompt


def test_build_prompt_says_so_when_truncated(wq):
    prompt = wq.build_prompt("why?", "y" * (wq.MAX_WORKLOG_CHARS + 1000))
    assert "truncated" in prompt.lower()


def test_build_prompt_does_not_mention_truncation_when_whole(wq):
    prompt = wq.build_prompt("why?", "## 2026-08-27\n\n- Short log.\n")
    assert "truncated" not in prompt.lower()


import subprocess


class _FakeCompleted:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_synthesize_returns_answer_on_success(wq, monkeypatch):
    monkeypatch.setattr(
        wq.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout="Because of the cutoff.\n")
    )
    result = wq.synthesize("prompt")
    assert result["ok"] is True
    assert result["answer"] == "Because of the cutoff."


def test_synthesize_never_raises_on_timeout(wq, monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120)

    monkeypatch.setattr(wq.subprocess, "run", _boom)
    result = wq.synthesize("prompt")
    assert result["ok"] is False
    assert "error" in result


def test_synthesize_never_raises_when_binary_missing(wq, monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("no such file or directory: 'claude'")

    monkeypatch.setattr(wq.subprocess, "run", _boom)
    result = wq.synthesize("prompt")
    assert result["ok"] is False
    assert "error" in result


def test_synthesize_reports_nonzero_exit(wq, monkeypatch):
    monkeypatch.setattr(
        wq.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout="", returncode=1)
    )
    result = wq.synthesize("prompt")
    assert result["ok"] is False


def test_answer_reports_missing_worklog_without_calling_claude(wq, monkeypatch):
    """No worklog means no question to answer — and crucially, no tokens
    spent finding that out."""
    calls = []
    monkeypatch.setattr(wq.subprocess, "run", lambda *a, **k: calls.append(a))
    result = wq.answer("why?")
    assert result["ok"] is False
    assert calls == []


def test_answer_returns_synthesis_result(wq, tmp_path, monkeypatch):
    _write_worklog(tmp_path, "## 2026-08-27\n\n- Chose acme because it was cheaper.\n")
    monkeypatch.setattr(
        wq.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout="Because it was cheaper.")
    )
    result = wq.answer("why acme?")
    assert result["ok"] is True
    assert result["answer"] == "Because it was cheaper."


def test_main_prints_the_answer_and_exits_zero(wq, tmp_path, monkeypatch, capsys):
    _write_worklog(tmp_path, "## 2026-08-27\n\n- Chose acme because it was cheaper.\n")
    monkeypatch.setattr(
        wq.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout="Because it was cheaper.")
    )
    code = wq.main(["why acme?"])
    assert code == 0
    assert "Because it was cheaper." in capsys.readouterr().out


def test_main_exits_nonzero_and_reports_the_error(wq, capsys):
    code = wq.main(["why acme?"])
    assert code == 1
    assert "no worklog" in capsys.readouterr().err
