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
