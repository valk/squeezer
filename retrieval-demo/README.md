# Demo SQUEEZER_HOME

Demo data for the worklog decision-retrieval feature. **This is not a real
worklog.** Point `SQUEEZER_HOME` here to try the feature without touching a
live squeezer install:

```
SQUEEZER_HOME=retrieval-demo python3 daemon/worklog_query.py \
  "why does elevation use a --settings overlay instead of --dangerously-skip-permissions?"
```

## Where the content comes from

`state/worklog.md` covers 2026-08-23 to 2026-09-05 and is reconstructed from
squeezer's own public commit history — the 2FA elevation work, the HUD
statusline rework, the budget-guard exemption, QR enrollment. Every entry
traces to a real merged commit in this repository; the prose was rewritten
into worklog form. Absolute home paths are genericized to `/Users/<user>/`.

A real `SQUEEZER_HOME` also holds `config.json`, `todos/`, `.env`, and
budget state. None of that is here, because the feature reads only
`state/worklog.md`.

## Why demo data instead of the real worklog

A live worklog contains registered project names, absolute home paths, and
personal names — none of which belong in a public repo (see `CLAUDE.md`).
Recordings and examples use this directory so nothing private ends up in a
screenshot or a committed file.

It also makes the feature reproducible: anyone who clones the repository can
run the same query and get a comparable answer, with no daemon, no Telegram
credentials, and no setup.

## Good questions to try

The log deliberately contains decisions with stated reasoning, one reversal,
and one explicitly rejected alternative:

- "why does elevation use a `--settings` overlay instead of
  `--dangerously-skip-permissions`?" — a rejected alternative, with the
  reasoning
- "why is the statusLine chained globally instead of SQUEEZER_HOME-only?"
- "why generate a QR code for 2FA setup?"
- "why was the squeezed percentage clamped?" — flagged on 2026-08-28, then
  root-caused properly on 2026-08-29, so the answer should cite the later
  entry
