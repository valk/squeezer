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
   candidate entries — no `--resume` of the long-lived orchestration
   session, no `--add-dir` project mounts, no growth in the prompt as the
   worklog grows.
3. **Non-perturbing.** Asking a question never consumes the orchestration
   session's context and never interleaves with the worker queue, so it
   cannot disturb in-flight work.
4. **Citable and deterministic where it counts.** Retrieval is pure Python
   and unit-tested; every answer names the `## date` and bullet index it
   came from. The generic path cites nothing and is reproducible only by
   chance.
5. **Standalone.** The CLI runs with no daemon, no Telegram credentials, and
   no MCP wiring — which is what makes it reproducible by someone who has
   only cloned the repo.

## Non-goals

- No embeddings, no vector store, no index to maintain. The corpus is 55KB
  (~14k tokens) over 16 days and grows at roughly 3.4KB/day; the entire
  history fits in a single context window today and will for about a year.
  Vector search here would be ceremony, not engineering. See "When to
  revisit" below for the trigger that would change this.
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

## Retrieval: recall-oriented by design

The central design constraint is a vocabulary gap. A user asks "why did we
drop the old provider?" while the worklog says "replace the vendor as a data
source before the subscription cutoff". The content words barely overlap.
For *why* questions this is the normal case, not the edge case: the question
is asked in the user's vocabulary and the log is written in the work's.

Therefore the ranker's job is **not** to find the answer. Its job is to
narrow the corpus to something prompt-sized without losing the answer, and
let the model do the semantic matching downstream. A confident,
precision-tuned lexical ranker is actively harmful here, because its
confident mistake is dropping the one entry that held the reasoning.

Scoring, per entry:

- **Weighted term overlap.** Lowercase, strip stopwords and interrogatives
  (*why*, *did*, *we*, *the*), crude suffix stemming. Terms are weighted by
  inverse document frequency computed over the worklog itself, so a term
  appearing in most entries contributes almost nothing while a rare one
  dominates.
- **Decision-marker boost.** A small additive bump for entries containing
  causal or decisional language: *because*, *rather than*, *instead of*,
  *decided*, *chose*, *opted*, *judged*, *per ESCALATION_POLICY*, *replied*,
  *proceed with*. Kept deliberately small so it breaks ties rather than
  dominating — otherwise every escalation note outranks the actual answer.
- **Recency tiebreak.** Slight preference for newer entries, since decisions
  get revisited and superseded.

Selection takes entries in score order until a token budget fills, subject
to a **floor**: entries keep being added until the budget is reached, but at
least `MIN_ENTRIES` are always included even when every score is zero. That
floor is the recall guarantee — a query sharing no vocabulary with the log
must still return candidates rather than nothing. Selected entries enter the
prompt in chronological order with their dates attached.

Two module-level constants, tunable in one place: `TOKEN_BUDGET = 6000` and
`MIN_ENTRIES = 15`. Token count is estimated as characters divided by four
rather than measured with a tokenizer — adding a tokenizer dependency to
approximate a budget that is itself a round guess would be false precision,
and the budget only needs to keep the prompt an order of magnitude below the
context limit. `MIN_ENTRIES` may exceed `TOKEN_BUDGET` for unusually long
entries; the floor wins, because returning too much beats returning nothing.

Two cheap precision knobs are available when the user already knows roughly
where to look: `--since <date>` and `--project <name>`.

## Synthesis

A single `claude -p` subprocess receives the selected entries and a prompt
instructing it to identify the decision and the reasoning behind it, to cite
the `## date` and bullet index, to prefer the **latest** decision when
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
work). Responsibilities: parse entries, rank them, select within budget,
build the prompt, invoke synthesis, format the cited answer.

**CLI.** A `__main__` entry point in that module. `--no-llm` prints the
ranked entries with no synthesis step at all — simultaneously the
deterministic fallback when `claude` is unavailable, the seam that keeps
tests free of subprocess calls, and an honest demonstration of what
retrieval alone produces before the model is involved.

**Telegram.** `TelegramCommand` gains `WHY`; `classify_command` matches
`/why …`; `_handle_telegram_message` handles it inline before the ordinary
message fallthrough, so nothing reaches the work queue.

One concurrency detail: `telegram_poll_loop` is a single thread, and a
synchronous 10–30 second synthesis inside it would block the bot from
seeing `/pause` for the duration. Synthesis and its reply therefore run on a
short-lived thread, keeping the poll loop responsive. This mirrors the
existing instant-command expectation that Telegram control never waits on
work.

**Refactor in scope.** `hud_status._last_insight()` moves onto the shared
parser rather than keeping its own `^## ` split. Its existing tests
(`tests/test_hud_status.py`) are the regression net proving behavior is
unchanged. This is the only refactor included; nothing else is touched.

## Testing

Following the convention in `tests/`: pytest, module loaded via importlib,
`SQUEEZER_HOME` monkeypatched to a scratch `tmp_path`, fixtures using
placeholder names (`acme`, `example-project`) only — never real project
names, paths, or usernames, per this repo's public-repo policy in
`CLAUDE.md`.

The deterministic core is what gets covered:

- **Parser.** Multi-day file yields the expected entries and dates; wrapped
  continuation lines attach to their bullet rather than starting a new
  entry; indented sub-bullets attach to their parent; a missing file, an
  empty file, and a file with no `## ` headings each degrade quietly.
- **Ranking.** A rare term outranks a common one; an entry with
  decision-marker language beats a bland mention of the same terms; recency
  breaks ties. Critically: **a zero-overlap query still returns the floor
  N**, pinning the recall guarantee against future tuning.
- **Selection.** Token budget respected; floor honored; output ordered
  chronologically.
- **Synthesis contract.** Never raises. Subprocess failure, timeout, and
  non-zero exit each return a structured error.
  `tests/test_usage_lib.py` already monkeypatches `subprocess.TimeoutExpired`
  and is the pattern to follow.
- **Integration properties.** `--no-llm` makes zero subprocess calls
  (asserted, not assumed). `/why …` classifies as `WHY` and puts **nothing**
  on the work queue — this assertion is what pins the "instant,
  non-perturbing" property so a later refactor cannot quietly regress it.

Explicitly not tested: answer quality from the real model. It is
non-deterministic and costs tokens on every run. One hand-checked question
is verified manually and the limitation is stated in the README rather than
papered over with a brittle assertion.

## Open risks, stated plainly

- **Retrieval quality is unmeasured.** There is no labelled question set and
  no recall metric. The floor guarantees candidates are returned; nothing
  guarantees the right entry is among them. For a corpus this size the
  practical risk is low, and it grows with the corpus.
- **The decision-marker list is hand-tuned.** It encodes the vocabulary of
  one worklog written under one operating policy. It will generalize
  imperfectly to a worklog written differently, and there is no mechanism
  that notices when it stops working.
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

The lexical approach should be replaced when recall degrades, not when the
file gets large — those are different thresholds. Parsing and ranking 1MB is
milliseconds, and the prompt stays constant size because only the selected
entries are sent, so growth alone changes nothing. The real signal is
queries returning candidate sets that plainly miss the relevant entry as
more entries compete on the same common terms. That is the point at which
BM25, or embeddings, finally earns the complexity it costs.
