# Worklog Decision Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ask "why did we choose X?" against squeezer's worklog and get back the decision, its reasoning, and the date it came from — as a standalone CLI and as an instant `/why` Telegram command.

**Architecture:** One new stdlib-only module, `daemon/worklog_query.py`, with four small functions: read the worklog, build a prompt containing the question and the whole log, shell out to `claude -p` for synthesis, and one `answer()` composing them. There is deliberately no ranking, parsing, or selection layer — the corpus is ~14k tokens against a 200k context window, so the entire log goes into the prompt and the model does all semantic matching. Telegram wiring adds a `WHY` command handled inline, off the work queue, on a short-lived thread.

**Tech Stack:** Python 3 standard library only (`subprocess`, `argparse`, `threading`, `pathlib`). pytest for tests. No new dependencies — the repo has no `requirements.txt` or `pyproject.toml` and stays that way.

**Spec:** `docs/superpowers/specs/2026-09-05-worklog-decision-retrieval-design.md`

## Global Constraints

- **Stdlib only.** No new third-party dependency, no `requirements.txt`, no `pyproject.toml`. Synthesis reuses the existing `claude -p` subprocess pattern rather than adding an SDK.
- **No new secret.** `templates/env.example` gains nothing.
- **Public repo — no real names.** No real project name, path, business detail, or local machine username in any tracked file. Test fixtures use `acme` / `example-project` and paths like `/absolute/path/to/example-project`. See `CLAUDE.md`.
- **Paths resolve through `daemon/config.py`.** Never hardcode `~/.config/squeezer`; use `_config.state_dir()` so `SQUEEZER_HOME` overrides work and tests can point at a scratch dir.
- **Tests follow the existing convention.** pytest under `tests/`, module loaded via `importlib`, `SQUEEZER_HOME` pointed at a scratch dir with `monkeypatch.setenv("SQUEEZER_HOME", ...)`. See `tests/test_config.py` and `tests/test_human_in_loop.py` for the pattern.
- **Run `python3 -m pytest tests/` before considering any task done.**
- **`hud_status.py` is not to be modified.** An earlier design draft refactored it; that is explicitly out of scope now.

---

### Task 1: Worklog reading and prompt construction

**Files:**
- Create: `daemon/worklog_query.py`
- Test: `tests/test_worklog_query.py`

**Interfaces:**
- Consumes: `daemon/config.py`'s `state_dir()` (existing).
- Produces:
  - `MAX_WORKLOG_CHARS: int` — size ceiling constant.
  - `read_worklog() -> str | None` — returns worklog text, or `None` if the file is missing or empty.
  - `build_prompt(question: str, worklog: str) -> str` — returns the full prompt string.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_worklog_query.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_worklog_query.py -v`
Expected: FAIL — the module file does not exist yet, so collection fails with `FileNotFoundError` / `ModuleNotFoundError`.

- [ ] **Step 3: Write the minimal implementation**

Create `daemon/worklog_query.py`:

```python
#!/usr/bin/env python3
"""Ask squeezer's worklog why a past decision was made.

SQUEEZER_HOME/state/worklog.md is the only durable record of *why* the
orchestrator did what it did — which task it picked and on what grounds,
what it escalated, what the user replied. Until now the only thing that
read it was hud_status._last_insight(), which returns the latest bullet
for the status line; everything else in the history was write-only.

There is deliberately no retrieval layer here. The worklog is ~14k tokens
against a 200k context window, so the whole file goes into the prompt and
the model does all of the semantic matching — recall is 100% by
construction, and there is no ranker to tune or parser edge case to drop
an entry. See the design doc for the measurement behind that decision:
docs/superpowers/specs/2026-09-05-worklog-decision-retrieval-design.md
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as _config

# ponytail: whole-log-in-prompt has a ceiling; at ~3.4KB/day this is years
# out. When it binds, prompt-cache a stable log prefix before reaching for
# a ranker — see "When to revisit" in the design doc.
MAX_WORKLOG_CHARS = 400_000

_PROMPT_TEMPLATE = """\
You are answering a question about a software project's worklog.

The worklog records what was done and, crucially, *why* — which options
were considered, which were rejected, and on what grounds.

Rules for your answer:
- Identify the decision and the reasoning behind it.
- Cite the `## <date>` heading the answer came from. Never answer without
  a citation.
- If several entries conflict, prefer the LATEST decision, and say
  explicitly that an earlier one was reversed.
- If the reasoning genuinely is not recorded, say "not found in the
  worklog". Do not speculate.
- Be brief. A few sentences, not an essay.
{truncation_note}
Question: {question}

--- WORKLOG ---
{worklog}
--- END WORKLOG ---
"""

_TRUNCATION_NOTE = """\
- NOTE: the worklog was truncated to its most recent portion because of
  its size. If the answer may lie in older history, say so.
"""


def read_worklog() -> str | None:
    """The worklog text, or None if there is nothing to read."""
    path = _config.state_dir() / "worklog.md"
    if not path.exists():
        return None
    text = path.read_text()
    return text if text.strip() else None


def build_prompt(question: str, worklog: str) -> str:
    """Prompt carrying the question and the worklog. Oversized logs keep
    their TAIL — the most recent history, where decisions relevant to a
    current question overwhelmingly live."""
    truncated = len(worklog) > MAX_WORKLOG_CHARS
    if truncated:
        worklog = worklog[-MAX_WORKLOG_CHARS:]
    return _PROMPT_TEMPLATE.format(
        truncation_note=_TRUNCATION_NOTE if truncated else "",
        question=question,
        worklog=worklog,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_worklog_query.py -v`
Expected: PASS — 7 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon/worklog_query.py tests/test_worklog_query.py
git commit -m "feat: read the worklog and build a decision-lookup prompt

Claude-Session: https://claude.ai/code/session_01S6kADHUgvZpruReZPcE3Ed"
```

---

### Task 2: Synthesis via `claude -p`

**Files:**
- Modify: `daemon/worklog_query.py` (append)
- Test: `tests/test_worklog_query.py` (append)

**Interfaces:**
- Consumes: `read_worklog()`, `build_prompt()` from Task 1.
- Produces:
  - `synthesize(prompt: str, timeout: int = 120) -> dict` — `{"ok": True, "answer": str}` or `{"ok": False, "error": str}`. Never raises.
  - `answer(question: str) -> dict` — same shape; composes read + build + synthesize.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worklog_query.py`:

```python
import subprocess


class _FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_worklog_query.py -v`
Expected: FAIL with `AttributeError: module 'worklog_query' has no attribute 'synthesize'`.

- [ ] **Step 3: Write the minimal implementation**

Add `import subprocess` to the imports at the top of `daemon/worklog_query.py`, then append:

```python
def synthesize(prompt: str, timeout: int = 120) -> dict:
    """Run the prompt through `claude -p`. Follows
    usage_lib.self_calibrate's contract: never raises, always returns a
    dict with "ok" set — a missing or broken `claude` binary degrades to a
    message rather than a traceback.

    Unlike daemon.py's spawn_claude this deliberately does NOT --resume the
    orchestration session or --add-dir any project: a human asking a
    question must never perturb in-flight work.
    """
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": False, "error": f"claude -p failed to run: {e}"}

    if proc.returncode != 0:
        return {"ok": False, "error": f"claude -p exited {proc.returncode}: {proc.stderr.strip()}"}

    return {"ok": True, "answer": proc.stdout.strip()}


def answer(question: str) -> dict:
    """Full path: read the worklog, ask, return {"ok", "answer"|"error"}."""
    worklog = read_worklog()
    if worklog is None:
        return {"ok": False, "error": "no worklog yet — SQUEEZER_HOME/state/worklog.md is missing or empty"}
    return synthesize(build_prompt(question, worklog))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_worklog_query.py -v`
Expected: PASS — 13 passed.

- [ ] **Step 5: Commit**

```bash
git add daemon/worklog_query.py tests/test_worklog_query.py
git commit -m "feat: synthesize a cited worklog answer via claude -p

Claude-Session: https://claude.ai/code/session_01S6kADHUgvZpruReZPcE3Ed"
```

---

### Task 3: CLI entry point

**Files:**
- Modify: `daemon/worklog_query.py` (append)
- Test: `tests/test_worklog_query.py` (append)

**Interfaces:**
- Consumes: `answer()` from Task 2.
- Produces: `main(argv: list[str] | None = None) -> int` — process exit code, 0 on success and 1 on failure.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worklog_query.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_worklog_query.py -v`
Expected: FAIL with `AttributeError: module 'worklog_query' has no attribute 'main'`.

- [ ] **Step 3: Write the minimal implementation**

Add `import argparse` to the imports at the top of `daemon/worklog_query.py`, then append:

```python
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask squeezer's worklog why a past decision was made."
    )
    parser.add_argument("question", help='e.g. "why did we drop the old provider?"')
    args = parser.parse_args(argv)

    result = answer(args.question)
    if not result["ok"]:
        print(result["error"], file=sys.stderr)
        return 1
    print(result["answer"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_worklog_query.py -v`
Expected: PASS — 15 passed.

- [ ] **Step 5: Verify the CLI runs end to end by hand**

Run against the demo worklog (not the live one):

```bash
SQUEEZER_HOME=/path/to/demo-squeezer-home python3 daemon/worklog_query.py \
  "why does elevation use a --settings overlay instead of --dangerously-skip-permissions?"
```

Expected: a few sentences naming the sandbox/credential-exposure reasoning, citing `## 2026-09-04`.

- [ ] **Step 6: Commit**

```bash
git add daemon/worklog_query.py tests/test_worklog_query.py
git commit -m "feat: add worklog_query CLI entry point

Claude-Session: https://claude.ai/code/session_01S6kADHUgvZpruReZPcE3Ed"
```

---

### Task 4: Telegram `/why` command

**Files:**
- Modify: `daemon/daemon.py` — `TelegramCommand` (line ~51), `classify_command` (line ~61), `_handle_telegram_message` (line ~580)
- Test: `tests/test_daemon.py` (append)

**Interfaces:**
- Consumes: `worklog_query.answer()` from Task 2.
- Produces: `TelegramCommand.WHY`; a `WHY` branch in `_handle_telegram_message` that replies asynchronously and never touches the work queue.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py`:

```python
# --- /why command ---

def test_classify_command_recognizes_why():
    assert daemon_mod.classify_command("/why did we pick acme?") == daemon_mod.TelegramCommand.WHY


def test_classify_command_why_is_case_insensitive():
    assert daemon_mod.classify_command("/WHY did we pick acme?") == daemon_mod.TelegramCommand.WHY


def test_plain_question_is_still_an_ordinary_message():
    """Only the explicit /why command takes the instant path — a bare
    question still goes to the orchestration turn as before."""
    assert daemon_mod.classify_command("why did we pick acme?") == daemon_mod.TelegramCommand.MESSAGE


def test_why_command_never_reaches_the_work_queue(monkeypatch):
    """The whole point of the instant path: asking a question must not
    queue work behind a possibly-long-running turn, and must not perturb
    the orchestration session."""
    sent = []
    monkeypatch.setattr(
        daemon_mod.telegram_lib, "send_message", lambda text, cfg=None, **k: sent.append(text)
    )
    monkeypatch.setattr(
        daemon_mod.worklog_query, "answer", lambda q: {"ok": True, "answer": "Because of the cutoff."}
    )

    work_queue = queue.Queue()
    before = set(threading.enumerate())
    daemon_mod._handle_telegram_message("/why did we pick acme?", None, work_queue, threading.Event())
    for thread in set(threading.enumerate()) - before:
        thread.join(timeout=5)

    assert work_queue.empty()
    assert any("Because of the cutoff." in text for text in sent)


def test_why_command_with_no_question_asks_for_one(monkeypatch):
    sent = []
    monkeypatch.setattr(
        daemon_mod.telegram_lib, "send_message", lambda text, cfg=None, **k: sent.append(text)
    )

    work_queue = queue.Queue()
    daemon_mod._handle_telegram_message("/why", None, work_queue, threading.Event())

    assert work_queue.empty()
    assert sent and "question" in sent[0].lower()
```

Note on the test style here: `tests/test_daemon.py` imports the module once at
file scope as a global named `daemon_mod` (via importlib) rather than using a
pytest fixture, and already imports `queue` and `threading` at the top. These
tests follow that — do not add a fixture or duplicate those imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_daemon.py -k why -v`
Expected: FAIL with `AttributeError: WHY`.

- [ ] **Step 3: Add the enum member and classifier branch**

In `daemon/daemon.py`, add to `TelegramCommand`:

```python
    WHY = "why"
```

In `classify_command`, add before the final `return TelegramCommand.MESSAGE`:

```python
    if stripped.startswith("/why"):
        return TelegramCommand.WHY
```

- [ ] **Step 4: Add the handler branch**

Add `import worklog_query  # noqa: E402` alongside the other `daemon/` module imports at the top of `daemon/daemon.py` (after the `sys.path.insert`, keeping the block alphabetical — it goes after `usage_lib`). `threading` is already imported there; do not re-import it.

In `_handle_telegram_message`, add before the "Ordinary message" fallthrough:

```python
    if command == TelegramCommand.WHY:
        question = text.strip()[len("/why"):].strip()
        if not question:
            telegram_lib.send_message("Ask me a question — e.g. /why did we pick acme?", cfg)
            return

        # Answered on a throwaway thread: telegram_poll_loop is a single
        # thread, and a synchronous 10-30s synthesis here would block the
        # bot from even seeing /pause for the duration. Never queued —
        # asking a question must not perturb in-flight work.
        def _answer_and_reply():
            result = worklog_query.answer(question)
            reply = result["answer"] if result["ok"] else f"Couldn't answer: {result['error']}"
            try:
                telegram_lib.send_message(reply, cfg)
            except Exception as e:  # noqa: BLE001 - a failed reply must not kill the thread
                log(f"could not send /why reply: {e}")

        log(f"answering /why: {question!r}")
        threading.Thread(target=_answer_and_reply, daemon=True).start()
        return
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_daemon.py -k why -v`
Expected: PASS — 5 passed.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest tests/`
Expected: all tests pass, including the pre-existing ones. `classify_command` gained a branch, so confirm no existing classifier test regressed.

- [ ] **Step 7: Commit**

```bash
git add daemon/daemon.py tests/test_daemon.py
git commit -m "feat: add instant /why Telegram command for worklog questions

Claude-Session: https://claude.ai/code/session_01S6kADHUgvZpruReZPcE3Ed"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above. Produces no code.

- [ ] **Step 1: Add a README section**

Add a section documenting the feature. It must cover:

- What it does: ask why a past decision was made, get the reasoning plus the date it came from.
- The CLI invocation, with a worked example question and answer.
- The `/why` Telegram command.
- **The honest limitations**, stated plainly rather than buried: answer quality is unmeasured and untested (the model's reading of the log is not verified by any assertion); every query sends the whole worklog, so cost scales with log size, not question size; the feature can only surface reasoning that was actually written down.
- That it has only been tested on macOS.

- [ ] **Step 2: Verify the documented commands actually run**

Copy each command out of the README and run it verbatim against the demo `SQUEEZER_HOME`. A README command that does not run as written is worse than no README.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document the worklog /why feature and its limits

Claude-Session: https://claude.ai/code/session_01S6kADHUgvZpruReZPcE3Ed"
```

---

## Notes for the executor

**On the missing ranker.** If you find yourself wanting to add scoring, filtering, or entry parsing because sending the whole worklog "feels wasteful" — don't. That layer was designed, costed, and deliberately cut after measuring the corpus at ~14k tokens against a 200k window. The reasoning is in the spec's "Why there is no retrieval layer". Adding it back silently would undo the most considered decision in this design.

**On `hud_status.py`.** Leave it alone. An earlier draft refactored it onto a shared parser; there is no shared parser any more, and touching working code for its own sake is not in scope.

**On demo data.** Never point tests, examples, or recordings at the live `~/.config/squeezer` — it contains real project names, absolute home paths, and personal names. Use a throwaway `SQUEEZER_HOME`.
