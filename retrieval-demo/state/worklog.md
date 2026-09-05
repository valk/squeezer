# Squeezer worklog

## 2026-08-23 — Initial commit

First commit of squeezer to its own repo. Nothing to log yet — this is the
baseline the rest of this history builds on.

## 2026-08-27 01:33 IDT — First real work cycle

**Context:** Fresh install. No real tasks queued yet for the one registered
project.

**Bug found and fixed: `squeezer-telegram` MCP server couldn't start.**

- `daemon.log` showed repeated `claude -p failed: ... No such file or
  directory: 'claude'` from 01:27–01:32. Root cause: the launchd plist
  (`~/Library/LaunchAgents/com.squeezer.daemon.plist`) didn't have
  `/Users/<user>/.local/bin` (where the real `claude` binary lives) on its
  `PATH`.
- Separately: the `squeezer-telegram` MCP server (`mcp/telegram_server.py`)
  was failing to start because its `.mcp.json` invoked bare `"python3"`,
  which resolves to `/opt/homebrew/bin/python3` in a normal shell — and that
  interpreter doesn't have the `mcp` package installed (confirmed:
  `ModuleNotFoundError: No module named 'mcp.server'`). `pip install mcp`
  into that interpreter is blocked by Homebrew's PEP 668 external-management
  guard. The daemon's own `python_bin` (pinned via `sys.executable` at
  `install_service.py` install time) *does* have `mcp` installed.
  - **Decision:** changed `.mcp.json`'s `command` from `"python3"` to the
    known-good absolute interpreter path matching the daemon plist, rather
    than trying to get `mcp` installed system-wide — the daemon already
    proved that interpreter works, so pointing at it directly is one line
    versus fighting Homebrew's guard.
  - **Follow-up decision (2026-09-01, `7d318db`):** made this permanent by
    having `/squeezer:setup` install the `mcp` package into the daemon's own
    interpreter during setup itself, instead of leaving it for whoever hits
    the failure first to diagnose by hand.

## 2026-08-27 — HUD statusline day: usage bars, colors, and why it's global

Several related decisions made in one sitting:

- **Decision:** rebuilt the HUD status bar to show squeezer's own
  budget-relative usage (share of its own allowed reserve used, and how
  large that reserve is versus the full window) instead of just the raw
  window total. Reason: the raw total didn't distinguish "the human is
  using their own quota normally" from "squeezer's own spend is eating the
  reserve" — the number that actually matters for deciding whether to
  intervene is the second one.
- **Decision:** reworked the bar into a four-zone view and simplified its
  labels after the first version's colors and descriptions were confusing
  at a glance — legibility over precision for a line users only glance at.
- **Decision:** chained squeezer's statusLine into Claude Code's *global*
  settings instead of leaving it SQUEEZER_HOME-only, and documented why:
  a SQUEEZER_HOME-scoped statusLine only renders in sessions whose cwd is
  inside SQUEEZER_HOME, which is nearly never true for real work — chaining
  globally (composing with `claude-hud` rather than overriding it) is the
  only way the status line is actually visible where a human would see it.
- **Decision:** added self-heal wiring for the daemon/statusLine plus a
  `/squeezer:uninstall` command — reasoning: setup already handled the
  install side, but nothing handled cleanup or repairing a wiring that
  silently rotted (e.g. after a plugin reinstall changes paths).

## 2026-08-28 — Ack messages, self-heal, and calibrating usage correctly

- **Decision:** `_handle_telegram_message` now sends an instant Telegram ack
  the moment a message is queued, before any `claude -p` turn runs. Reason:
  a queued message otherwise sits silent until the current turn finishes
  (which can take a while), and that reads as a hang rather than "received,
  working on it."
- **Decision:** self-heal a stale daemon service after a plugin
  reinstall/update, rather than requiring a manual restart. A silently-dead
  daemon after an update is a worse failure mode than a few extra seconds
  of self-check on every relevant trigger.
- **Decision:** calibrate the usage window from combined human+squeezer
  tokens, not from squeezer's own transcript alone. Reason found while
  debugging the HUD bar showing wrong percentages: squeezer's own turns are
  a subset of what's actually consuming the window, so calibrating from
  only its own transcript systematically undercounts.
- **Decision:** clamp the HUD's "squeezed: N%" to `[0, 100]` after noticing
  it could print values outside that range — flagged here, fully root-caused
  and fixed properly the next day (see 2026-08-29).

## 2026-08-29 03:43 IDT — Fixed "squeezed >100%" bug, then found squeezer had been self-paused since 08-27

**Squeezed-percentage fix** (`[Telegram/User]`: "Why the status shows:
squeezed 108%?" then "Make squeezed only from 0 to 100..."): traced it to
`daemon/hud_status.py`'s `_squeezer_usage_percents` — `of_budget` is
squeezer's usage as a % of its own *allowed max*, recomputed fresh every
render; if the human's own direct usage grows mid-window it shrinks that
allowed max retroactively, so squeezer's already-spent tokens can exceed
the new smaller max and print e.g. "108%". Real and self-correcting, just
never capped for display.

**Decision:** fixed `_squeezer_usage_percents` to clamp `of_budget` to
`[0, 100]` at render time rather than trying to prevent the underlying
number from ever exceeding 100 — the underlying "spent more than the
currently-recomputed allowance" state is real and informative internally
(e.g. for logs), only the human-facing display needed the ceiling.

**Separately, found squeezer had self-paused since 08-27 02:36** (loop
breaker: too many consecutive continuation turns with no change to
`todos/`/`worklog.md`, assuming it was stuck re-nagging a blocked item).
**Decision:** left the self-pause mechanism as-is rather than loosening the
trigger — a false-positive pause that requires one `/resume` is a much
cheaper failure than a genuine stuck loop burning budget unnoticed.

**Related decision this same day:** let a declined paused-recheck ask be
snoozed to a specific check-back time, instead of only offering an
immediate yes/no — reasoning: "not now, but ask me again at 6pm" is a real
answer a blanket no/yes pair can't express.

## 2026-09-04 — Telegram TOTP elevation: why 2FA, and why this design specifically

Full day implementing 2FA elevation for the `/elevate` Telegram command —
worked from a design spec written first (`docs/superpowers/specs/...`),
then an implementation plan, then code.

- **Decision: require a second factor (TOTP) for elevation, not just
  Telegram's existing chat-id/user-id lock.** Reasoning recorded in the
  spec: a single compromised or leaked Telegram session is enough to defeat
  a chat-id-only gate; TOTP means an attacker also needs the physical
  authenticator device/secret, a materially different bar.
- **Decision: `autoMode.hard_deny` stays completely untouched — elevation
  never attempts to lift it.** Investigated Claude Code's permission system
  first: `hard_deny` is documented as unconditional by design, with no
  supported runtime override; the only way to lift one is editing the
  settings file itself, a deliberate manual act. Treated this as the
  correct, permanent floor rather than something to route around.
- **Decision: rejected `--dangerously-skip-permissions` as the mechanism**,
  even though it looks like it would trivially satisfy "elevate for a
  window." Reasoning: it's not scoped — Read/Edit/Write tools aren't
  sandboxed at all, only gated by the permission system, so bypassing it
  entirely could read `~/.ssh/id_rsa` or overwrite `~/.zshrc`. Not an
  acceptable trade for the convenience.
- **Decision: implement elevation as a scoped `autoMode.allow` overlay
  passed via the `--settings` CLI flag**, so it never touches
  `~/.claude/settings.json` and never leaks into the human's own
  interactive sessions. This only lifts `soft_deny`-class actions, which
  are documented as liftable by an `allow` exception — the honest, buildable
  version of "elevate," not a bypass.
- **Decision: TOTP built stdlib-only (RFC 6238), including rate limiting**
  (5 failures / 5 min → 15 min lockout) and replay protection via
  last-used-step tracking — rather than adding a TOTP library dependency
  for what is, underneath, a small, well-specified hash construction.
- **Follow-up fix same day:** `parse_elevate_command` was accepting
  non-ASCII digit characters (e.g. Unicode lookalikes) as a valid 6-digit
  code — rejected explicitly after the review pass caught it; a fuzzy digit
  match would have quietly widened the code space TOTP is supposed to
  narrow.
- **Follow-up fix same day:** elevation state persistence now fails safe on
  a malformed `expires_at` (treats it as already-expired) instead of
  raising — a corrupt timestamp should never fail *open*.
- Also fixed, unrelated but same session: state JSON is now written
  atomically, recovering from truncated files left by a process killed
  mid-write.
- **Decision (`38277ab`): a fully-squeezed turn should still be able to
  answer a Telegram status question**, per the user's explicit request.
  Before this, `budget_guard.sh` denied *every* tool call once the reserve
  was breached, including `telegram_send` itself — so a squeezed turn was
  completely unreachable over Telegram, not just restricted from doing real
  work, which meant the human had no way to even ask "why aren't you
  responding?" without first manually raising the reserve blind.
  Implemented a narrow exemption (status-reply and a single explicit
  `override-reserve` command only) rather than loosening the guard
  generally, specifically so a squeezed turn can *explain itself and ask*
  for more budget without being able to quietly resume real work on its own
  authority.

## 2026-09-05 — QR code for 2FA enrollment

**Decision:** generate a scannable QR image during `/squeezer:2fa-setup`
enrollment instead of only printing the raw TOTP secret/URI as text.
Reasoning: every authenticator app supports "scan a QR code" as the primary
enrollment path, and manually retyping a base32 secret is exactly the kind
of step that gets fat-fingered once and then silently locks enrollment —
generating the image removes that failure mode for close to no added code.
