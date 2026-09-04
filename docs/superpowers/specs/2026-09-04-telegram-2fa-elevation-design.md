# Telegram TOTP elevation for squeezer

## Motivation

squeezer's headless `claude -p --permission-mode auto` turns are governed by
the `autoMode` classifier policy (`allow` / `soft_deny` / `hard_deny`,
configured per-project in `~/.claude/settings.json`) plus squeezer's own
`ESCALATION_POLICY.md`. Today, anything the policy blocks either proceeds
autonomously (if allowed), gets silently denied (headless `-p` has no prompt
to fall back to), or — per `ESCALATION_POLICY.md`'s Axis 2 — is escalated to
the user over Telegram as a judgment question.

The user wants a way to remotely authorize a *wider* scope of autonomous
action for a bounded window (2/4/8/24 hours), gated behind a second factor
(TOTP, e.g. Google Authenticator) rather than just Telegram's existing
chat-id/user-id lock — so a single compromised/leaked Telegram session isn't
enough to unlock it.

## What this can and can't actually do

Investigation into how Claude Code's permission system works (current docs,
2026-09-04) rules out a literal "perform anything" implementation on bare
metal:

- **`autoMode.hard_deny` is unconditional by design** — "User intent and
  `allow` exceptions don't apply." There is no supported runtime override;
  the only way to lift one is to edit the settings file that defines it,
  which is a deliberate manual act, not a grant. **This spec does not
  attempt to lift `hard_deny`, and treats that as correct, permanent
  behavior**, matching the floor the user explicitly asked to keep.
- **`bypassPermissions` (`--dangerously-skip-permissions`) is not a safe
  substitute.** Anthropic's own docs: *"Only use this mode in isolated
  environments like containers, VMs... offers no protection against prompt
  injection."* It also does not preserve a credential floor the way it
  sounds like it should: Read/Edit/Write tools aren't sandboxed at all (only
  Bash subprocesses are — see `sandboxing` docs), they're gated purely by
  the permission system, which `bypassPermissions` switches off entirely.
  An elevated `bypassPermissions` window could read `~/.ssh/id_rsa` or
  overwrite `~/.zshrc` straight through the Read/Write tools. **Not used
  here.**
- **`autoMode.soft_deny` *is* liftable, by design**, two ways:
  1. Explicit, specific user intent stated in the conversation ("run the
     prod deploy now") — the classifier honors this today, per docs, with
     zero code changes. squeezer already relays a Telegram reply as the next
     `--resume`'d turn's prompt, so a specific enough escalation reply
     should already cross a soft_deny wall.
  2. An `autoMode.allow` entry, which is documented to override a matching
     `soft_deny` rule as an exception ("the combination is additive").

Given that, "elevation" in this spec means: **for the approved window, add a
scoped `autoMode.allow`/`environment` overlay (via the `--settings` CLI
flag, so it never touches `~/.claude/settings.json` or leaks into the user's
own interactive sessions) that gives the classifier strong, explicit,
time-boxed authorization to cross `soft_deny`-class actions — while
`hard_deny` and all existing `permissions.deny`/sandbox/credential
protections remain completely untouched.** This is the honest, buildable
version of "perform anything [short of the hard floor]" — it works by
influencing a judgment-based classifier with a strong authorization
statement, not by a deterministic bypass switch, and that distinction is
called out to the user in the confirmation message every time elevation is
granted.

## Non-goals

- Not a general filesystem-directory grant (adding new project directories
  to squeezer's scope is a separate, already-adequate flow: register the
  project in `config.json`, or use Claude Code's own `/add-dir` inside an
  interactive session). Elevation does not touch `--add-dir`.
- Not a way to lift `hard_deny` or any `permissions.deny` rule, ever.
- Not a QR-code enrollment flow for v1 — manual base32 key entry into an
  authenticator app is standard and sufficient.

## TOTP mechanics

RFC 6238 TOTP, implemented with the Python standard library only (`hmac`,
`hashlib`, `base64`, `struct`, `time`) — squeezer has zero third-party
Python dependencies today (`daemon/*.py` imports stdlib plus its own
modules only) and this preserves that.

- **Secret**: a 160-bit random secret (`secrets.token_bytes(20)`), base32-
  encoded for display, generated once during setup and stored in
  `SQUEEZER_HOME/.env` as `TOTP_SECRET=...` — same file, same gitignored
  pattern as the existing Telegram bot token (`daemon/config.py`'s
  `load_env()` already reads this file).
- **Verification**: standard 30-second step, accept the current step and
  one step on either side (±30s clock-skew tolerance — a narrower window
  than typical libraries default to, since this is a locally-generated
  secret with no distribution latency to account for).
- **Replay protection**: track the last successfully-used step index in
  `state/totp.json`; a code for a step at or before that index is rejected
  even if numerically valid.
- **Rate limiting**: 5 failed attempts within 5 minutes locks out further
  attempts for 15 minutes (tracked in the same state file). This is mostly
  defense-in-depth — brute-forcing a 6-digit code one Telegram message at a
  time is already impractical — but it costs little to add.

## Enrollment

Extend `/squeezer:setup` (or add `/squeezer:2fa-setup` as its own idempotent
step) to:

1. Generate the secret if `SQUEEZER_HOME/.env` doesn't already have one.
2. Print the base32 secret and an `otpauth://totp/squeezer:<label>?secret=...&issuer=squeezer`
   URI for manual entry into Google Authenticator (or any TOTP app).
3. Ask the user to confirm by entering the current 6-digit code back, so a
   typo in manual key entry is caught immediately rather than at 2am when
   the code never validates.

## Telegram command UX

Extends `classify_command`'s existing deterministic-command pattern
(`/pause`, `/resume`, `/auto`, `/manual`) with two more:

- **`/elevate <code> <hours>`** — `hours` must be one of `2`, `4`, `8`,
  `24`. On success: writes `state/elevation.json` with `expires_at`, sends a
  confirmation Telegram message stating the exact expiry time *and*
  explicitly restating that hard_deny/credential protections stay in force.
  On a bad code: generic "invalid or expired code" reply (doesn't reveal
  whether the code was well-formed but wrong vs. rate-limited, to avoid
  leaking state to anyone who isn't the authenticated Telegram user — though
  note the whole channel is already locked to the owner's chat_id/user_id,
  so this is a minor extra precaution).
- **`/lockdown`** — ends an active elevation immediately, regardless of
  remaining time. Always available, no code required (revocation should
  never be harder than granting).

Both follow the existing `_handle_telegram_message` short-circuit pattern:
handled directly in the poll loop, never queued as work for `claude -p`.

## Integration points

- **`daemon/totp.py`** (new, pure functions only, mirroring the
  `human_in_loop.py` split): `generate_secret`, `provisioning_uri`,
  `verify_code(secret, code, last_used_step, now) -> (bool, new_last_used_step | None)`,
  `parse_elevate_command(text) -> (code, hours) | None`.
- **`daemon/config.py`**: add `load_elevation_state` / `save_elevation_state`
  helpers following the existing `state_dir()` / JSON-file pattern used for
  `load_hil_state`/`save_hil_state` (moved here or kept alongside them in
  `daemon.py` — implementation detail to settle while coding).
- **`daemon.py`**:
  - `classify_command` gains `ELEVATE` and `LOCKDOWN` cases (with the
    `/elevate ...` argument captured, since existing commands are all
    zero-argument — `classify_command`'s return type needs to carry the
    parsed args now, or elevate handling parses `text` itself the way
    `parse_budget_cap` already does for free-text replies).
  - `_handle_telegram_message` gains the two new branches.
  - `build_claude_command` (or `spawn_claude`, which calls it) checks
    `state/elevation.json`; while `now < expires_at`, it writes a small
    overlay JSON to a temp path under `state/` and adds
    `--settings <overlay path>` to the command. The overlay contains only:
    ```json
    {
      "autoMode": {
        "allow": [
          "$defaults",
          "The user explicitly authorized crossing routine soft-deny protections (deploys, pushes, and similar destructive-but-reversible operations within registered projects) via a verified 2FA elevation, valid until <ISO timestamp>. This does not apply to anything in hard_deny."
        ]
      }
    }
    ```
    Never sets `hard_deny`, `soft_deny`, `environment`, or anything outside
    `autoMode.allow`.

## Audit trail

- Grant and lockdown both produce an immediate Telegram confirmation
  (already covers the "known when it started/stopped" need without a
  separate proactive expiry notification — natural lapse is silent, matching
  the YAGNI bar for v1; can add a proactive "elevation has lapsed" ping
  later if it turns out to matter in practice).
- `log()` (existing daemon logger) records every grant, lockdown, and failed
  TOTP attempt with a timestamp, alongside everything else the daemon
  already logs.

## Testing

Per this repo's `CLAUDE.md` convention (`tests/test_human_in_loop.py`,
`tests/test_config.py` pattern — `importlib`-load a `daemon/*.py` module,
point it at a scratch `SQUEEZER_HOME` via `monkeypatch.setenv`):

- `tests/test_totp.py`: known-answer RFC 6238 test vectors, step-window
  acceptance/rejection, replay rejection, rate-limit lockout and its expiry.
- `tests/test_daemon.py` (or extend existing coverage): `classify_command`
  recognizes `/elevate 123456 8` and `/lockdown`; `build_claude_command`
  includes `--settings <overlay>` only while elevation is active and omits
  it once expired; the overlay's content never includes `hard_deny` or
  `soft_deny` keys.

## Empirical verification: the overlay mechanism actually works

Before shipping, this was checked directly rather than assumed (2026-09-04,
Claude Code v2.1.260): a scratch `--settings` overlay containing only
`{"autoMode": {"allow": ["$defaults", "<distinctive marker>"]}}` was passed
to `claude --settings <overlay> auto-mode config` alongside this machine's
real `~/.claude/settings.json` (which already carries deltasharpe's
project-specific `hard_deny`/`allow` entries via `automode_sync.py`). Result:

- The overlay's marker string appeared in the effective `allow` list
  (count went from 20 to 21) — confirming a `--settings`-supplied
  `autoMode.allow` block genuinely is read and merged by the classifier
  config, not silently ignored.
- The effective `hard_deny` list was byte-for-byte unchanged (still 13
  entries), and every one of deltasharpe's existing hard_deny rules
  (force-push, `rm -rf`, prod docker/ansible, `.env` read/write) was still
  present. An `allow`-only overlay does not replace or widen `hard_deny` —
  it merges additively, exactly as this feature's entire safety argument
  requires.

This resolves the one scenario that could have turned this feature from
"ineffective" into "actively dangerous" (a `--settings` overlay silently
replacing the whole `autoMode` block, wiping `hard_deny` during elevation).
It doesn't.

## Open risks, stated plainly

- This is influence over a judgment-based classifier, not a deterministic
  switch — the classifier could, in principle, still decline something even
  during an active elevation if its own reasoning doesn't line up with the
  authorization text. That's the correct failure mode (fails closed), but
  it means "elevated" is not a hard guarantee of "will proceed."
- A compromised phone/authenticator app is now a path to broader autonomous
  action (bounded by `hard_deny`) for up to 24 hours. This is the accepted
  trade-off of building the feature at all; `/lockdown` and the existing
  Telegram chat-id/user-id lock are the mitigations.
- If Anthropic changes how `--settings`-supplied `autoMode.allow` combines
  with user-settings-defined `soft_deny`/`hard_deny` in a future Claude Code
  release, this mechanism's effectiveness could silently change. No
  automated way to detect that; worth an occasional manual sanity check
  (`claude auto-mode config` with `--settings` applied) after Claude Code
  upgrades.
