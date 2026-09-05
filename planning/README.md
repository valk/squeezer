# Planning artifacts

Working notes and handoff artifacts for the worklog decision-retrieval
feature — asking squeezer's worklog *why* a past decision was made.

## Read in this order

1. **`2026-09-05-worklog-decision-retrieval-design.md`** — the design spec.
   Scope, what it adds over what already existed, and the reasoning behind
   each decision. The section worth reading first is "Why there is no
   retrieval layer".
2. **`2026-09-05-worklog-decision-retrieval.md`** — the implementation plan.
   Five TDD tasks, each with its tests, its code, and a commit.

## What changed between them

The first spec specified a full retrieval layer: an entry parser,
IDF-weighted term overlap, a decision-marker boost, a recency tiebreak, and
token-budgeted selection with a minimum-entry floor.

All of it was cut before any code was written, after measuring the corpus —
~13.8k tokens against a 200k-token context window. The whole worklog fits in
one prompt, so every one of those components could only make recall *worse*
than sending everything. The spec keeps the analysis rather than deleting
it, and records the two thresholds that would bring a ranker back.

That arc is visible in the git history:

- `98cafe3` — design spec, full ranking version
- `59c1105` — implementation plan, and the ranker cut

## Related

Squeezer's own pre-existing specs and plans live under `docs/superpowers/`,
which is this repo's established convention. This folder holds the
assessment's planning artifacts specifically.
