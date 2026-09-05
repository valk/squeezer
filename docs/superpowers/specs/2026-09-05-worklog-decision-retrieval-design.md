# Worklog decision retrieval for squeezer

## Motivation

squeezer accumulates a narrative operating history in
`SQUEEZER_HOME/state/worklog.md` — one `## YYYY-MM-DD` section per day,
top-level `- ` bullets per entry, with wrapped continuation lines and
indented sub-bullets. The operating policy in `CLAUDE.md.template` tells
each turn to flush its work there before ending, so the file is the only
durable record of *why* the orchestrator did what it did: which task it
picked and on what grounds, what it escalated and under which
`ESCALATION_POLICY.md` axis, what the user replied over Telegram, and what
it deliberately chose not to do.

Exactly one thing reads that file today: `hud_status._last_insight()`, which
splits on `^## ` and returns the single most recent top-level bullet,
truncated to 70 characters for the HUD status line. Everything else in the
history is write-only. After a few weeks the questions that matter most —
"why did we drop the old provider?", "what were we waiting on before the
cutoff?" — are answerable only by opening a 55KB file and reading.

This spec adds a way to ask those questions in natural language and get a
cited answer: the decision, the reasoning behind it, and the date and bullet
it came from.

## Scope: decision-and-rationale lookup, not summarization

The feature answers *why* questions. "Why did we choose X", "what was the
reasoning behind Y", "why is Z still blocked". It finds the decision point
and the reasoning attached to it.

It is deliberately not a general timeline summarizer ("what happened last
week"), not a cross-project status rollup, and not a chat interface over the
worklog. Those are all defensible features and all wider; this one is
picked because it is the narrowest thing that is genuinely useful and that
plain `grep` cannot do, since the reasoning for a decision is almost never
phrased in the words someone later uses to ask about it.

## What this adds over what already exists

A fair objection: sending "why did we drop the old provider?" to the
Telegram bot *today* already produces an answer. `classify_command`
(`daemon/daemon.py`) does not match it, so it falls through to
`TelegramCommand.MESSAGE`, gets queued, and the next `claude -p --resume`
turn answers it — that turn runs with `SQUEEZER_HOME` as its cwd and can
read `worklog.md` unaided.

That path works, and this feature has to earn its place against it. It does
so on five counts:

1. **Instant.** Handled inline in `_handle_telegram_message`, next to
   `/pause` and `/resume`, rather than queued behind a turn that may run up
   to `CLAUDE_SPAWN_TIMEOUT`.
2. **Bounded cost.** One focused `claude -p` carrying only the selected
   worklog — no `--resume` of the long-lived orchestration session, whose
   accumulated context dwarfs the worklog, and no `--add-dir` project
   mounts. The prompt does grow with the worklog; see "Open risks."
3. **Non-perturbing.** Asking a question never consumes the orchestration
   session's context and never interleaves with the worker queue, so it
   cannot disturb in-flight work.
4. **Citable.** Every answer names the `## date` heading it came from, and
   the prompt forbids answering without one. The generic path cites nothing.
5. **Standalone.** The CLI runs with no daemon, no Telegram credentials, and
   no MCP wiring — which is what makes it reproducible by someone who has
   only cloned the repo.

## Non-goals

- No embeddings, no vector store, no index to maintain.
- **No ranking or selection layer at all.** The whole worklog goes into the
  prompt. See "Why there is no retrieval layer" below — this is the single
  biggest simplification in the design and it was made after measuring, not
  assumed.
- No change to how the worklog is written. Decisions are identified at read
  time, not tagged at write time.
- No new dependency. The repo is stdlib-only with no `requirements.txt` or
  `pyproject.toml`, and stays that way. Synthesis reuses the established
  `claude -p` subprocess pattern rather than adding an SDK and an API key.
- No new secret. `templates/env.example` gains nothing.

## Why read-time extraction, not write-time tagging

Tagging decisions as they are written — say, a `- **Decision:** … **Because:**
…` convention added to `CLAUDE.md.template` — would give cleaner, more
reliably parseable data than inferring structure after the fact.

It is still the wrong choice here. It changes the operating policy of a
running system, it only takes effect for entries written after the change,
and it makes the existing history invisible to the feature that is supposed
to search it. Read-time extraction works on every entry ever written,
including all of the current file, and costs nothing if the convention later
changes.

## Why there is no retrieval layer

The first version of this design had one: an entry parser, inverse-document-
frequency-weighted term overlap, a decision-marker boost, a recency
tiebreak, and token-budgeted selection with a minimum-entry floor. All of it
was cut before implementation. The reasoning is recorded here because the
cut is the most important decision in the design.

The motivating constraint was real. There is a genuine vocabulary gap: a
user asks "why did we drop the old provider?" while the worklog says
"replace the vendor as a data source before the subscription cutoff." The
content words barely overlap, which is the normal case for *why* questions —
they are asked in the user's vocabulary while the log is written in the
work's. That argued for a recall-oriented ranker whose job was to narrow the
corpus without losing the answer, since a precision-tuned lexical ranker's
characteristic failure is confidently dropping the one entry that held the
reasoning.

Then the corpus was measured: **55,241 bytes, roughly 13.8k tokens, against
a 200k-token context window.** The entire history fits in a single prompt
with an order of magnitude to spare. Every component above existed to solve
a problem the numbers say does not exist — and each one could only make
recall *worse* than sending everything, never better. A ranker that selects
6k tokens out of 13.8k is not an optimization; it is a way to lose 60% of
the corpus in exchange for a token saving nobody needs on a hand-triggered
query.

So the whole worklog goes into the prompt, and the model does all of the
semantic matching. Recall is 100% by construction. There is no ranker to
tune, no scoring function to regress, and no parser edge case (wrapped
continuation lines, indented sub-bullets, malformed headings) that can drop
an entry, because nothing is ever selecting between entries.

The one guard retained is a size ceiling: if the worklog ever exceeds
`MAX_WORKLOG_CHARS`, the **tail** is kept — the most recent history, which
is where decisions relevant to a current question overwhelmingly live — and
the prompt is told the log was truncated so the model can say so rather than
silently answering from a partial record. At the current growth rate of
~3.4KB/day that ceiling is years away. It exists so the failure mode is a
stated truncation instead of a confusing context-limit error.

## Synthesis

A single `claude -p` subprocess receives the worklog and a prompt
instructing it to identify the decision and the reasoning behind it, to cite
the `## date` heading it came from, to prefer the **latest** decision when
several conflict and say so explicitly if an earlier one was reversed, and
to answer "not found in the worklog" rather than speculate when the
reasoning genuinely is not recorded.

Supersession matters more here than it might appear. "Why did we choose X"
has a wrong-but-plausible answer whenever an early exploratory decision was
later reversed, and undated synthesis over a chronological log reliably
produces exactly that error.

The synthesis function follows `usage_lib.self_calibrate`'s contract: it
never raises. Subprocess failure, timeout, and non-zero exit each return a
structured result with `ok` set, so a broken or absent `claude` binary
degrades to a clear message rather than a traceback.

### Budget interaction

`hooks/budget_guard.sh` blocks tool calls once the configured reserve is
breached, but only for turns whose cwd is `SQUEEZER_HOME` — that is, only
squeezer's own daemon-spawned turns. This is deliberate: the reserve exists
to protect the human's quota, not to gate it.

A human-initiated worklog query is correctly outside that gate. It spends
the human's own quota, exactly like an interactive session does, and is not
subject to the daemon's reserve. Note that this differs from the existing
out-of-band `claude -p` precedent in `usage_lib.self_calibrate`, which is
justified by `/usage` being rendered client-side and costing no meaningful
tokens. Synthesis does cost tokens. The cost is small and human-triggered,
but it is real and is stated here rather than glossed.

## Integration points

**New module: `daemon/worklog_query.py`.** Stdlib only, following the
existing `daemon/*.py` convention (importable, testable via importlib,
resolving paths through `daemon/config.py` so `SQUEEZER_HOME` overrides
work). Four small functions: read the worklog, build the prompt, invoke
synthesis, and one `answer()` that composes them.

**CLI.** A `__main__` entry point in that module, taking the question as its
argument.

**Telegram.** `TelegramCommand` gains `WHY`; `classify_command` matches
`/why …`; `_handle_telegram_message` handles it inline before the ordinary
message fallthrough, so nothing reaches the work queue.

One concurrency detail: `telegram_poll_loop` is a single thread, and a
synchronous 10–30 second synthesis inside it would block the bot from
seeing `/pause` for the duration. Synthesis and its reply therefore run on a
short-lived thread, keeping the poll loop responsive. This mirrors the
existing instant-command expectation that Telegram control never waits on
work.

**No refactor in scope.** An earlier draft moved
`hud_status._last_insight()` onto a shared entry parser. With no parser
left, there is nothing to share, and touching working code for its own sake
is not worth the regression risk. `hud_status.py` is untouched.

## Testing

Following the convention in `tests/`: pytest, module loaded via importlib,
`SQUEEZER_HOME` monkeypatched to a scratch `tmp_path`, fixtures using
placeholder names (`acme`, `example-project`) only — never real project
names, paths, or usernames, per this repo's public-repo policy in
`CLAUDE.md`.

Cutting the ranker cut most of the test surface with it — there is no
scoring function to pin and no parser edge case to cover. What remains:

- **Prompt construction.** The built prompt contains the question and the
  worklog text.
- **Truncation.** A worklog over `MAX_WORKLOG_CHARS` keeps the **tail**, not
  the head, and the prompt says it was truncated. This is the one piece of
  non-obvious logic in the module, and getting it backwards would silently
  discard the most recent history — exactly the entries most likely to hold
  the answer.
- **Synthesis contract.** Never raises. Subprocess failure, timeout, and
  non-zero exit each return a structured error.
  `tests/test_usage_lib.py` already monkeypatches `subprocess.TimeoutExpired`
  and is the pattern to follow.
- **Missing worklog.** No file, or an empty one, produces a clear message
  rather than a traceback or an empty prompt sent to the model.
- **Telegram wiring.** `/why …` classifies as `WHY` and puts **nothing** on
  the work queue — this assertion is what pins the "instant, non-perturbing"
  property so a later refactor cannot quietly regress it.

Explicitly not tested: answer quality from the real model. It is
non-deterministic and costs tokens on every run. One hand-checked question
is verified manually and the limitation is stated in the README rather than
papered over with a brittle assertion.

## Open risks, stated plainly

- **Answer quality is unmeasured.** There is no labelled question set and no
  accuracy metric. Recall is 100% by construction — the whole log is in the
  prompt — but nothing verifies the model reads it correctly, and a
  confidently wrong citation is the failure mode to watch for.
- **Cost scales with the corpus, not the question.** Every query sends the
  entire worklog, so a one-line question costs ~14k input tokens today and
  more as the log grows. Prompt caching is not exploited. This is the
  deliberate price of deleting the ranker, and it is the first thing to
  revisit if query volume ever rises.
- **Answer quality depends on the worklog being honest.** If a turn recorded
  what it did without recording why, no retrieval strategy recovers the
  reasoning. The feature surfaces what was written; it cannot surface what
  was not.
- **Synthesis is unmetered.** Nothing rate-limits `/why`. A user who sends
  it in a loop spends their own quota with no guard. Acceptable for a
  single-user tool triggered by hand; it would not be for a shared one.
- **Demo data must be sanitized.** The live worklog contains real project
  names, absolute home paths, and the user's own name. Any recording,
  screenshot, or exported transcript must use a throwaway `SQUEEZER_HOME`
  seeded from `templates/`, not the live one.

## When to revisit

Add a retrieval layer when one of two things is actually true, not before:

1. **The worklog stops fitting comfortably in a prompt.** At ~3.4KB/day the
   log reaches 200k tokens somewhere past the five-year mark, and
   `MAX_WORKLOG_CHARS` truncation covers the interim by keeping the most
   recent history. This threshold is far away and easy to observe.
2. **Per-query cost starts to matter** — many queries a day, or a much
   larger log, turning ~14k input tokens per question into a real number.
   The cheapest fix at that point is prompt caching over a stable log
   prefix, not a ranker.

Note which threshold is *absent* from that list: file size alone. A 1MB
worklog reads and concatenates in milliseconds; size only matters through
its effect on the prompt. Conflating "the file is big" with "we need
retrieval" is what produced the over-built first draft of this design.

When a ranker does become necessary, the first draft's approach — recall-
oriented lexical scoring with a decision-marker boost, deliberately never
precision-tuned — is recorded above and is the place to start. BM25 and
embeddings come after that, if it proves insufficient.
