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
planning/2026-09-05-worklog-decision-retrieval-design.md
"""
import argparse
import subprocess
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
