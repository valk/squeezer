# Telegram TOTP Elevation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner remotely widen squeezer's autonomous action for a bounded window (2/4/8/24h) via a TOTP-gated `/elevate <code> <hours>` Telegram command, without ever touching `autoMode.hard_deny` or any credential/sandbox protection.

**Architecture:** A new pure-function module `daemon/totp.py` implements RFC 6238 TOTP (stdlib only — `hmac`/`hashlib`/`base64`/`struct`/`secrets`), replay/rate-limit decision logic, and `/elevate`+`/lockdown` command parsing, mirroring the existing `daemon/human_in_loop.py` split (pure logic, no I/O). `daemon/daemon.py` owns all state persistence and wiring: new `state/totp.json` and `state/elevation.json` files (same pattern as the existing `load_hil_state`/`save_hil_state`), two new `TelegramCommand` cases, and a `--settings <overlay>` flag appended to `build_claude_command`'s output whenever an elevation is currently active.

**Tech Stack:** Python 3 stdlib only (no new dependencies — squeezer has zero third-party Python deps today), pytest, existing `importlib`-based test-loading pattern.

**Spec:** `docs/superpowers/specs/2026-09-04-telegram-2fa-elevation-design.md`

## Global Constraints

- Zero new third-party dependencies — TOTP is implemented with stdlib `hmac`/`hashlib`/`base64`/`struct`/`secrets`/`time` only.
- `autoMode.hard_deny`, `soft_deny` (as a section), `environment`, and every `permissions.deny`/`sandbox.credentials` rule are NEVER written by this feature — the elevation overlay's `autoMode` key contains only `allow`.
- The elevation overlay is passed via `--settings <path>` and never modifies `~/.claude/settings.json` or any file outside `SQUEEZER_HOME`.
- No real names, paths with the local username, or secrets in anything committed to this repo (it's public) — code must use `SQUEEZER_HOME`-relative paths and `${CLAUDE_PLUGIN_ROOT}` in command files, never a hardcoded `/Users/<name>/...` path.
- Every new function that does no I/O goes in `daemon/totp.py` and is unit tested with fixed/injected `now` values — never a live `time.time()` call inside a test.
- Follow this repo's existing test pattern exactly: `importlib.util.spec_from_file_location` loading the module fresh in each test file (see `tests/test_daemon.py` and `tests/test_human_in_loop.py`), `tmp_path` + `monkeypatch.setenv("SQUEEZER_HOME", ...)` for anything touching state files.
- Run `python3 -m pytest tests/` after every task and before considering the plan done (this repo's `CLAUDE.md` convention).

---

### Task 1: TOTP secret and code generation

**Files:**
- Create: `daemon/totp.py`
- Test: `tests/test_totp.py`

**Interfaces:**
- Produces: `generate_secret(length: int = 20) -> str` (base32 string), `provisioning_uri(secret: str, label: str = "squeezer", issuer: str = "squeezer") -> str`, `current_step(now: float, period: int = 30) -> int`, `totp_at_step(secret: str, step: int, digits: int = 6) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for daemon/totp.py — RFC 6238 TOTP (stdlib only), replay/rate-limit
decision logic, and /elevate + /lockdown command parsing. Pure functions,
no I/O: every input, including "now", is passed in explicitly so tests never
depend on wall-clock time."""
import base64
import importlib.util
from pathlib import Path

SQUEEZER_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("totp", SQUEEZER_DIR / "daemon" / "totp.py")
totp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(totp)


# --- generate_secret / provisioning_uri ---

def test_generate_secret_is_valid_base32():
    secret = totp.generate_secret()
    # must decode cleanly as base32 with no padding errors
    base64.b32decode(secret)


def test_generate_secret_is_random():
    assert totp.generate_secret() != totp.generate_secret()


def test_provisioning_uri_contains_secret_and_issuer():
    uri = totp.provisioning_uri("JBSWY3DPEHPK3PXP", label="squeezer", issuer="squeezer")
    assert uri.startswith("otpauth://totp/")
    assert "secret=JBSWY3DPEHPK3PXP" in uri
    assert "issuer=squeezer" in uri


# --- current_step ---

def test_current_step_boundaries():
    assert totp.current_step(0) == 0
    assert totp.current_step(29) == 0
    assert totp.current_step(30) == 1
    assert totp.current_step(59) == 1
    assert totp.current_step(60) == 2


# --- totp_at_step against RFC 6238 Appendix B known-answer vectors ---
# Secret is the ASCII string "12345678901234567890", base32-encoded here
# (RFC 6238 states the secret in ASCII/hex; we feed our own base32 form
# since that's what generate_secret/provisioning_uri produce and consume).
# The RFC's table gives 8-digit codes; the last 6 digits are what a 6-digit
# TOTP (Google Authenticator's default) produces, since both are the same
# HOTP value truncated to a different digit count.

_RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_totp_matches_rfc6238_vector_at_time_59():
    # RFC 6238 Appendix B: T=59s -> time-step 1 -> HOTP 94287082 -> last 6: 287082
    assert totp.totp_at_step(_RFC_SECRET, step=1) == "287082"


def test_totp_matches_rfc6238_vector_at_time_1111111109():
    # T=1111111109s -> time-step 37037036 -> HOTP 07081804 -> last 6: 081804
    assert totp.totp_at_step(_RFC_SECRET, step=37037036) == "081804"


def test_totp_at_step_is_six_digits_zero_padded():
    # Regression guard: a leading-zero code must not get truncated to 5 chars.
    code = totp.totp_at_step(_RFC_SECRET, step=1)
    assert len(code) == 6
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /path/to/squeezer && python3 -m pytest tests/test_totp.py -v`
Expected: FAIL — `daemon/totp.py` doesn't exist yet (`ModuleNotFoundError` / `spec_from_file_location` returning a spec whose loader errors on `exec_module`).

- [ ] **Step 3: Write the implementation**

```python
"""RFC 6238 TOTP (stdlib only — no third-party dependency), replay/rate-limit
decision logic, and /elevate + /lockdown Telegram command parsing. Pure
functions only: every timestamp is a parameter, never read from the clock
directly, so this module has no I/O and no hidden state — daemon.py owns
persistence (see load_totp_state/save_totp_state, load_elevation_state/
save_elevation_state) and calls into these functions with what it read."""
import base64
import hashlib
import hmac
import secrets
import struct


def generate_secret(length: int = 20) -> str:
    """A random secret, base32-encoded for display and for entry into an
    authenticator app. 20 bytes (160 bits) matches Google Authenticator's
    own default key length."""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii")


def provisioning_uri(secret: str, label: str = "squeezer", issuer: str = "squeezer") -> str:
    """An otpauth:// URI for QR-code or manual-entry enrollment in any TOTP
    app (Google Authenticator, etc.)."""
    return (
        f"otpauth://totp/{issuer}:{label}?secret={secret}&issuer={issuer}"
        f"&algorithm=SHA1&digits=6&period=30"
    )


def current_step(now: float, period: int = 30) -> int:
    """The RFC 6238 time-step counter for `now` (a time.time()-style epoch
    seconds float)."""
    return int(now // period)


def totp_at_step(secret: str, step: int, digits: int = 6) -> str:
    """The zero-padded `digits`-digit TOTP code for a given time-step
    counter (RFC 6238's HOTP-over-a-counter construction, SHA1)."""
    key = base64.b32decode(secret.upper())
    msg = struct.pack(">Q", step)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code_int).zfill(digits)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_totp.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add daemon/totp.py tests/test_totp.py
git commit -m "feat: add stdlib-only RFC 6238 TOTP code generation"
```

---

### Task 2: TOTP verification with time-window and replay protection

**Files:**
- Modify: `daemon/totp.py`
- Test: `tests/test_totp.py`

**Interfaces:**
- Consumes: `current_step`, `totp_at_step` from Task 1.
- Produces: `verify_code(secret: str, code: str, last_used_step: int | None, now: float, period: int = 30) -> tuple[bool, int | None]` — `(True, matched_step)` on success, `(False, None)` on failure. Later tasks (daemon.py wiring) persist `matched_step` as the new `last_used_step`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_totp.py`:

```python
# --- verify_code ---

_SECRET = totp.generate_secret()


def test_verify_code_accepts_current_step():
    now = 1_000_000.0
    step = totp.current_step(now)
    code = totp.totp_at_step(_SECRET, step)
    ok, matched = totp.verify_code(_SECRET, code, last_used_step=None, now=now)
    assert ok is True
    assert matched == step


def test_verify_code_accepts_one_step_early_or_late():
    now = 1_000_000.0
    step = totp.current_step(now)
    prev_code = totp.totp_at_step(_SECRET, step - 1)
    next_code = totp.totp_at_step(_SECRET, step + 1)
    assert totp.verify_code(_SECRET, prev_code, None, now)[0] is True
    assert totp.verify_code(_SECRET, next_code, None, now)[0] is True


def test_verify_code_rejects_two_steps_away():
    now = 1_000_000.0
    step = totp.current_step(now)
    far_code = totp.totp_at_step(_SECRET, step - 2)
    ok, matched = totp.verify_code(_SECRET, far_code, None, now)
    assert ok is False
    assert matched is None


def test_verify_code_rejects_wrong_code():
    now = 1_000_000.0
    ok, matched = totp.verify_code(_SECRET, "000000", None, now)
    assert ok is False
    assert matched is None


def test_verify_code_rejects_replay_of_already_used_step():
    now = 1_000_000.0
    step = totp.current_step(now)
    code = totp.totp_at_step(_SECRET, step)
    # last_used_step == this step: already spent, must not verify again
    ok, matched = totp.verify_code(_SECRET, code, last_used_step=step, now=now)
    assert ok is False
    assert matched is None


def test_verify_code_rejects_step_before_last_used():
    now = 1_000_000.0
    step = totp.current_step(now)
    old_code = totp.totp_at_step(_SECRET, step - 1)
    ok, matched = totp.verify_code(_SECRET, old_code, last_used_step=step, now=now)
    assert ok is False
    assert matched is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_totp.py -v -k verify_code`
Expected: FAIL — `AttributeError: module 'totp' has no attribute 'verify_code'`

- [ ] **Step 3: Write the implementation**

Append to `daemon/totp.py`:

```python
def verify_code(
    secret: str, code: str, last_used_step: int | None, now: float, period: int = 30
) -> tuple[bool, int | None]:
    """Accepts the current time-step and one step on either side (a total
    90-second window — generous enough for clock skew and Telegram
    round-trip, narrow enough to keep the search space at 3 codes). A step
    at or before `last_used_step` is rejected even if the code is
    numerically correct, so a captured code can't be replayed."""
    step = current_step(now, period)
    for candidate in (step - 1, step, step + 1):
        if last_used_step is not None and candidate <= last_used_step:
            continue
        if hmac.compare_digest(totp_at_step(secret, candidate), code):
            return True, candidate
    return False, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_totp.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add daemon/totp.py tests/test_totp.py
git commit -m "feat: add TOTP verification with step-window and replay protection"
```

---

### Task 3: TOTP rate limiting

**Files:**
- Modify: `daemon/totp.py`
- Test: `tests/test_totp.py`

**Interfaces:**
- Produces: `record_failed_attempt(state: dict, now: float, max_attempts: int = 5, window_seconds: int = 300, lockout_seconds: int = 900) -> dict` (returns a new state dict — never mutates the input), `is_locked_out(state: dict, now: float) -> bool`. `state` shape: `{"failed_attempts": list[float], "locked_until": float | None}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_totp.py`:

```python
# --- rate limiting ---

def test_is_locked_out_false_for_empty_state():
    assert totp.is_locked_out({"failed_attempts": [], "locked_until": None}, now=1000.0) is False


def test_record_failed_attempt_accumulates_without_locking_below_threshold():
    state = {"failed_attempts": [], "locked_until": None}
    now = 1000.0
    for i in range(4):
        state = totp.record_failed_attempt(state, now=now + i)
    assert len(state["failed_attempts"]) == 4
    assert state["locked_until"] is None
    assert totp.is_locked_out(state, now=now + 4) is False


def test_record_failed_attempt_locks_out_at_fifth_failure_within_window():
    state = {"failed_attempts": [], "locked_until": None}
    now = 1000.0
    for i in range(5):
        state = totp.record_failed_attempt(state, now=now + i)
    assert state["locked_until"] == now + 4 + 900
    assert totp.is_locked_out(state, now=now + 4) is True


def test_lockout_expires_after_lockout_seconds():
    state = {"failed_attempts": [], "locked_until": None}
    now = 1000.0
    for i in range(5):
        state = totp.record_failed_attempt(state, now=now + i)
    locked_until = state["locked_until"]
    assert totp.is_locked_out(state, now=locked_until - 1) is True
    assert totp.is_locked_out(state, now=locked_until + 1) is False


def test_old_failures_outside_window_do_not_count_toward_lockout():
    state = {"failed_attempts": [], "locked_until": None}
    # 4 failures, then a 6-minute gap (past the 300s window), then 1 more —
    # should NOT reach the 5-in-5-minutes threshold.
    now = 1000.0
    for i in range(4):
        state = totp.record_failed_attempt(state, now=now + i)
    state = totp.record_failed_attempt(state, now=now + 4 + 360)
    assert state["locked_until"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_totp.py -v -k "locked_out or record_failed"`
Expected: FAIL — `AttributeError: module 'totp' has no attribute 'record_failed_attempt'`

- [ ] **Step 3: Write the implementation**

Append to `daemon/totp.py`:

```python
def record_failed_attempt(
    state: dict, now: float, max_attempts: int = 5, window_seconds: int = 300, lockout_seconds: int = 900
) -> dict:
    """Records one failed verification attempt and returns a new state dict
    (the input is never mutated). Failures older than `window_seconds` are
    dropped before counting. Reaching `max_attempts` within the window sets
    `locked_until`; the attempt list is cleared at that point since
    `locked_until` alone gates further attempts from there."""
    recent = [t for t in state.get("failed_attempts", []) if now - t < window_seconds]
    recent.append(now)
    locked_until = state.get("locked_until")
    if len(recent) >= max_attempts:
        locked_until = now + lockout_seconds
        recent = []
    return {"failed_attempts": recent, "locked_until": locked_until}


def is_locked_out(state: dict, now: float) -> bool:
    locked_until = state.get("locked_until")
    return locked_until is not None and now < locked_until
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_totp.py -v`
Expected: PASS (18 tests)

- [ ] **Step 5: Commit**

```bash
git add daemon/totp.py tests/test_totp.py
git commit -m "feat: add TOTP rate limiting (5 failures / 5 min -> 15 min lockout)"
```

---

### Task 4: `/elevate` + `/lockdown` command parsing and elevation overlay content

**Files:**
- Modify: `daemon/totp.py`
- Test: `tests/test_totp.py`

**Interfaces:**
- Produces: `parse_elevate_command(text: str) -> tuple[str, int] | None` (returns `(code, hours)`, or `None` if the text isn't a well-formed `/elevate` command), `build_elevation_overlay(expires_at_iso: str) -> dict` (the JSON-able `--settings` overlay content).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_totp.py`:

```python
# --- parse_elevate_command ---

def test_parse_elevate_command_valid():
    assert totp.parse_elevate_command("/elevate 123456 8") == ("123456", 8)


def test_parse_elevate_command_valid_with_extra_whitespace():
    assert totp.parse_elevate_command("  /elevate   123456   24  ") == ("123456", 24)


def test_parse_elevate_command_rejects_non_menu_hours():
    assert totp.parse_elevate_command("/elevate 123456 6") is None


def test_parse_elevate_command_rejects_non_numeric_code():
    assert totp.parse_elevate_command("/elevate abcdef 8") is None


def test_parse_elevate_command_rejects_missing_hours():
    assert totp.parse_elevate_command("/elevate 123456") is None


def test_parse_elevate_command_rejects_non_elevate_text():
    assert totp.parse_elevate_command("please work on the AAPL task") is None


def test_parse_elevate_command_accepts_all_menu_hours():
    for hours in (2, 4, 8, 24):
        assert totp.parse_elevate_command(f"/elevate 123456 {hours}") == ("123456", hours)


# --- build_elevation_overlay ---

def test_build_elevation_overlay_contains_only_auto_mode_allow():
    overlay = totp.build_elevation_overlay("2026-09-04T18:00:00+00:00")
    assert set(overlay.keys()) == {"autoMode"}
    assert set(overlay["autoMode"].keys()) == {"allow"}


def test_build_elevation_overlay_keeps_defaults_and_states_expiry():
    overlay = totp.build_elevation_overlay("2026-09-04T18:00:00+00:00")
    allow = overlay["autoMode"]["allow"]
    assert "$defaults" in allow
    assert any("2026-09-04T18:00:00+00:00" in entry for entry in allow)
    assert any("hard_deny" in entry for entry in allow)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_totp.py -v -k "elevate_command or elevation_overlay"`
Expected: FAIL — `AttributeError: module 'totp' has no attribute 'parse_elevate_command'`

- [ ] **Step 3: Write the implementation**

Append to `daemon/totp.py`:

```python
_ELEVATE_HOURS_CHOICES = (2, 4, 8, 24)


def parse_elevate_command(text: str) -> tuple[str, int] | None:
    """Parses "/elevate <6-digit code> <hours>". `hours` must be one of
    2, 4, 8, or 24 — the fixed menu, not an arbitrary duration. Returns
    None for anything malformed, including ordinary non-command text."""
    parts = text.strip().split()
    if len(parts) != 3 or parts[0].lower() != "/elevate":
        return None
    code, hours_str = parts[1], parts[2]
    if not (code.isdigit() and len(code) == 6):
        return None
    if not hours_str.isdigit():
        return None
    hours = int(hours_str)
    if hours not in _ELEVATE_HOURS_CHOICES:
        return None
    return code, hours


def build_elevation_overlay(expires_at_iso: str) -> dict:
    """The --settings overlay content for an active elevation window. Only
    ever adds an autoMode.allow entry — never hard_deny, soft_deny,
    environment, or anything outside autoMode. See the design spec's
    "What this can and can't actually do" section for why hard_deny is
    never touched here."""
    return {
        "autoMode": {
            "allow": [
                "$defaults",
                (
                    "The user explicitly authorized crossing routine soft-deny "
                    "protections (deploys, pushes, and similar destructive-but-"
                    "reversible operations within registered projects) via a "
                    f"verified 2FA elevation, valid until {expires_at_iso}. "
                    "This does not apply to anything in hard_deny."
                ),
            ]
        }
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_totp.py -v`
Expected: PASS (28 tests)

- [ ] **Step 5: Commit**

```bash
git add daemon/totp.py tests/test_totp.py
git commit -m "feat: add /elevate command parsing and elevation overlay content"
```

---

### Task 5: daemon.py state persistence for TOTP and elevation

**Files:**
- Modify: `daemon/daemon.py` (add alongside the existing `# --- state persistence ---` section at the `load_hil_state`/`save_hil_state` functions, roughly `daemon.py:199-208`)
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: nothing new (uses the existing `_state_path` helper already in `daemon.py`).
- Produces: `load_totp_state() -> dict` (shape: `{"last_used_step": int | None, "failed_attempts": list[float], "locked_until": float | None}`), `save_totp_state(state: dict) -> None`, `load_elevation_state() -> dict` (shape: `{"expires_at": str | None}` — ISO timestamp or `None`), `save_elevation_state(state: dict) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py`:

```python
# --- TOTP / elevation state persistence ---

def test_load_totp_state_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    state = daemon_mod.load_totp_state()
    assert state == {"last_used_step": None, "failed_attempts": [], "locked_until": None}


def test_save_and_load_totp_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_totp_state({"last_used_step": 12345, "failed_attempts": [1.0, 2.0], "locked_until": None})
    assert daemon_mod.load_totp_state() == {"last_used_step": 12345, "failed_attempts": [1.0, 2.0], "locked_until": None}


def test_load_elevation_state_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    assert daemon_mod.load_elevation_state() == {"expires_at": None}


def test_save_and_load_elevation_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_elevation_state({"expires_at": "2026-09-04T18:00:00+00:00"})
    assert daemon_mod.load_elevation_state() == {"expires_at": "2026-09-04T18:00:00+00:00"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_daemon.py -v -k "totp_state or elevation_state"`
Expected: FAIL — `AttributeError: module 'daemon_mod' has no attribute 'load_totp_state'`

- [ ] **Step 3: Write the implementation**

In `daemon/daemon.py`, immediately after `save_hil_state` (currently ending at line 208), add:

```python
def load_totp_state() -> dict:
    path = _state_path("totp.json")
    if path.exists():
        return json.loads(path.read_text())
    return {"last_used_step": None, "failed_attempts": [], "locked_until": None}


def save_totp_state(state: dict) -> None:
    _state_path("totp.json").write_text(json.dumps(state, indent=2) + "\n")


def load_elevation_state() -> dict:
    path = _state_path("elevation.json")
    if path.exists():
        return json.loads(path.read_text())
    return {"expires_at": None}


def save_elevation_state(state: dict) -> None:
    _state_path("elevation.json").write_text(json.dumps(state, indent=2) + "\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_daemon.py -v`
Expected: PASS (all existing tests plus the 4 new ones)

- [ ] **Step 5: Commit**

```bash
git add daemon/daemon.py tests/test_daemon.py
git commit -m "feat: add TOTP and elevation state persistence to daemon.py"
```

---

### Task 6: Elevation overlay file + `build_claude_command`/`spawn_claude` wiring

**Files:**
- Modify: `daemon/daemon.py` — import `totp` (near the other `import ... as _config`-style imports at the top, `daemon.py:34-39`), add `current_elevation_overlay_path()`, extend `build_claude_command` (currently `daemon.py:70-76`) and `spawn_claude` (currently `daemon.py:217-243`).
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `totp.build_elevation_overlay` (Task 4), `load_elevation_state` (Task 5).
- Produces: `current_elevation_overlay_path(now: datetime | None = None) -> str | None`. `build_claude_command(prompt, session_id, project_paths, settings_overlay_path=None) -> list[str]` — new 4th optional parameter, backward compatible with every existing call site and test.

- [ ] **Step 1: Write the failing tests**

At the top of `tests/test_daemon.py`, add `import json` (not currently
imported there) and add `timezone` to the existing datetime import:

```python
import json
from datetime import datetime, timedelta, timezone
```

Then append to `tests/test_daemon.py`:

```python
# --- build_claude_command settings overlay ---

def test_build_claude_command_without_overlay_omits_settings_flag():
    cmd = daemon_mod.build_claude_command("hi", None, [])
    assert "--settings" not in cmd


def test_build_claude_command_with_overlay_appends_settings_flag():
    cmd = daemon_mod.build_claude_command("hi", None, [], settings_overlay_path="/tmp/overlay.json")
    assert "--settings" in cmd
    assert cmd[cmd.index("--settings") + 1] == "/tmp/overlay.json"


# --- current_elevation_overlay_path ---

def test_current_elevation_overlay_path_none_when_no_elevation(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    assert daemon_mod.current_elevation_overlay_path(now=datetime(2026, 9, 4, 12, 0, 0)) is None


def test_current_elevation_overlay_path_none_when_expired(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_elevation_state({"expires_at": "2026-09-04T10:00:00+00:00"})
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    assert daemon_mod.current_elevation_overlay_path(now=now) is None


def test_current_elevation_overlay_path_writes_overlay_when_active(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZER_HOME", str(tmp_path))
    daemon_mod.save_elevation_state({"expires_at": "2026-09-04T18:00:00+00:00"})
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    path = daemon_mod.current_elevation_overlay_path(now=now)
    assert path is not None
    content = json.loads(Path(path).read_text())
    assert content["autoMode"]["allow"]
    assert "hard_deny" in content["autoMode"]["allow"][-1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_daemon.py -v -k "overlay"`
Expected: FAIL — `build_claude_command` raises `TypeError: unexpected keyword argument 'settings_overlay_path'`, and `current_elevation_overlay_path` doesn't exist.

- [ ] **Step 3: Write the implementation**

At the top of `tests/test_daemon.py`, the import block needs `timezone` alongside the existing `datetime`:

```python
from datetime import datetime, timedelta, timezone
```

In `daemon/daemon.py`, add the `totp` import next to the other daemon-module imports (`daemon.py:34-39`):

```python
import check_mcp_deps  # noqa: E402
import config as _config  # noqa: E402
import human_in_loop  # noqa: E402
import telegram_lib  # noqa: E402
import totp  # noqa: E402
import usage_lib  # noqa: E402
```

Also widen the existing `datetime` import at the top of `daemon.py` (currently `daemon.py:30`, `from datetime import datetime`) to add `timezone`:

```python
from datetime import datetime, timezone
```

Replace `build_claude_command` (`daemon.py:70-76`) with:

```python
def build_claude_command(
    prompt: str, session_id: str | None, project_paths: list[str], settings_overlay_path: str | None = None
) -> list[str]:
    cmd = ["claude", "-p", prompt, "--permission-mode", "auto", "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    for path in project_paths:
        cmd += ["--add-dir", path]
    if settings_overlay_path:
        cmd += ["--settings", settings_overlay_path]
    return cmd
```

Add, right after `save_elevation_state` (the function added in Task 5):

```python
def current_elevation_overlay_path(now: datetime | None = None) -> str | None:
    """None when no elevation is active or it has expired. Otherwise
    (re)writes state/elevation_overlay.json with the current expiry baked
    in and returns its path, so build_claude_command can pass it via
    --settings for this turn only."""
    now = now or datetime.now(timezone.utc)
    expires_at_iso = load_elevation_state().get("expires_at")
    if not expires_at_iso:
        return None
    expires_at = datetime.fromisoformat(expires_at_iso)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if now >= expires_at:
        return None
    overlay_path = _state_path("elevation_overlay.json")
    overlay_path.write_text(json.dumps(totp.build_elevation_overlay(expires_at_iso), indent=2) + "\n")
    return str(overlay_path)
```

In `spawn_claude` (`daemon.py:217-243`), change the `build_claude_command` call to pass the overlay:

```python
    session_state = load_session_state()
    project_paths = [p["path"] for p in _config.projects()]
    overlay_path = current_elevation_overlay_path()
    cmd = build_claude_command(prompt, session_state.get("session_id"), project_paths, overlay_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_daemon.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add daemon/daemon.py tests/test_daemon.py
git commit -m "feat: wire elevation overlay into build_claude_command/spawn_claude"
```

---

### Task 7: Telegram command handling — `/elevate` and `/lockdown`

**Files:**
- Modify: `daemon/daemon.py` — `TelegramCommand` enum (`daemon.py:49-54`), `classify_command` (`daemon.py:57-67`), `_handle_telegram_message` (`daemon.py:509-559`).
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `totp.parse_elevate_command`, `totp.verify_code`, `totp.record_failed_attempt`, `totp.is_locked_out` (Tasks 2-4); `load_totp_state`/`save_totp_state`, `load_elevation_state`/`save_elevation_state` (Task 5); `_config.load_env()` (existing, for `TOTP_SECRET`).
- Produces: two new `TelegramCommand` members (`ELEVATE`, `LOCKDOWN`); `classify_command` recognizes both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py`:

```python
# --- classify_command: elevate / lockdown ---

def test_classify_elevate_command():
    assert daemon_mod.classify_command("/elevate 123456 8") == daemon_mod.TelegramCommand.ELEVATE


def test_classify_elevate_with_bad_args_is_still_elevate():
    # classify_command only recognizes the shape; totp.parse_elevate_command
    # (already tested in test_totp.py) is what validates code/hours.
    assert daemon_mod.classify_command("/elevate nonsense") == daemon_mod.TelegramCommand.ELEVATE


def test_classify_lockdown_command():
    assert daemon_mod.classify_command("/lockdown") == daemon_mod.TelegramCommand.LOCKDOWN


def test_classify_ordinary_text_still_message_after_elevate_added():
    assert daemon_mod.classify_command("elevate my mood please") == daemon_mod.TelegramCommand.MESSAGE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_daemon.py -v -k "elevate_command or lockdown_command"`
Expected: FAIL — `AttributeError: ELEVATE` (enum member doesn't exist yet).

- [ ] **Step 3: Write the implementation**

Replace the `TelegramCommand` enum (`daemon.py:49-54`):

```python
class TelegramCommand(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    AUTO = "auto"
    MANUAL = "manual"
    ELEVATE = "elevate"
    LOCKDOWN = "lockdown"
    MESSAGE = "message"
```

Replace `classify_command` (`daemon.py:57-67`):

```python
def classify_command(text: str) -> TelegramCommand:
    stripped = text.strip().lower()
    if stripped in ("/pause", "/stop"):
        return TelegramCommand.PAUSE
    if stripped in ("/resume", "/start", "/continue"):
        return TelegramCommand.RESUME
    if stripped == "/auto":
        return TelegramCommand.AUTO
    if stripped in ("/manual", "/human"):
        return TelegramCommand.MANUAL
    if stripped.startswith("/elevate"):
        return TelegramCommand.ELEVATE
    if stripped == "/lockdown":
        return TelegramCommand.LOCKDOWN
    return TelegramCommand.MESSAGE
```

In `_handle_telegram_message` (`daemon.py:509-559`), add two branches right after the existing `MANUAL` branch (before the "Ordinary message" comment):

```python
    if command == TelegramCommand.ELEVATE:
        parsed = totp.parse_elevate_command(text)
        if parsed is None:
            telegram_lib.send_message(
                "Usage: /elevate <6-digit code> <hours>, hours one of 2, 4, 8, 24.", cfg
            )
            return
        code, hours = parsed
        totp_state = load_totp_state()
        now = time_mod.time()
        if totp.is_locked_out(totp_state, now):
            log("ELEVATE rejected: rate-limited")
            telegram_lib.send_message("Too many recent failed codes — locked out for a bit. Try again shortly.", cfg)
            return
        secret = _config.load_env().get("TOTP_SECRET")
        if not secret:
            log("ELEVATE rejected: no TOTP_SECRET configured")
            telegram_lib.send_message("2FA isn't set up yet — run /squeezer:2fa-setup first.", cfg)
            return
        ok, matched_step = totp.verify_code(secret, code, totp_state.get("last_used_step"), now)
        if not ok:
            save_totp_state(totp.record_failed_attempt(totp_state, now))
            log("ELEVATE rejected: invalid code")
            telegram_lib.send_message("Invalid or expired code.", cfg)
            return
        totp_state["last_used_step"] = matched_step
        save_totp_state(totp_state)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        expires_at_iso = expires_at.isoformat()
        save_elevation_state({"expires_at": expires_at_iso})
        log(f"ELEVATE granted until {expires_at_iso}")
        telegram_lib.send_message(
            f"Elevated until {expires_at_iso} — soft-deny protections (deploys, pushes, etc.) "
            "may be crossed with your explicit authorization. hard_deny and all credential/"
            "sandbox protections remain fully in force. Send /lockdown to end this early.",
            cfg,
        )
        return

    if command == TelegramCommand.LOCKDOWN:
        save_elevation_state({"expires_at": None})
        log("LOCKDOWN: elevation ended")
        telegram_lib.send_message("Elevation ended.", cfg)
        return
```

This also uses `timedelta`, which isn't imported yet — Task 6 already widened `daemon.py`'s top-level datetime import to `from datetime import datetime, timezone`; widen it again to add `timedelta`:

```python
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (the full suite — this is the first point every prior task's tests run together with these)

- [ ] **Step 5: Commit**

```bash
git add daemon/daemon.py tests/test_daemon.py
git commit -m "feat: handle /elevate and /lockdown Telegram commands"
```

---

### Task 8: Enrollment — `templates/env.example` and `commands/2fa-setup.md`

**Files:**
- Modify: `templates/env.example`
- Create: `commands/2fa-setup.md`

**Interfaces:**
- Consumes: `totp.generate_secret`, `totp.provisioning_uri`, `totp.verify_code` (all already unit tested in Task 1/2) — this task wires them into a Claude Code slash command, not new Python, so there's no new pytest coverage; verification is a manual dry run (Step 4 below).

- [ ] **Step 1: Add `TOTP_SECRET` to the env template**

Append to `templates/env.example`:

```
# Base32 TOTP secret for the /elevate Telegram command's second factor.
# Generated by /squeezer:2fa-setup — do not hand-write this value.
TOTP_SECRET=
```

- [ ] **Step 2: Write the enrollment command**

Create `commands/2fa-setup.md`:

```markdown
---
description: Enroll a TOTP secret (Google Authenticator or similar) for squeezer's /elevate Telegram command.
---

Set up 2FA for squeezer's `/elevate` command. Idempotent — safe to re-run
(it won't regenerate an existing secret unless the user explicitly asks to
replace it). Use the Bash tool for each step.

1. `SQUEEZER_HOME="${SQUEEZER_HOME:-$HOME/.config/squeezer}"`.
2. Check whether `$SQUEEZER_HOME/.env` already has a `TOTP_SECRET=` line
   with a non-empty value. If it does, tell the user 2FA is already set up
   and ask whether they want to replace it (only proceed past this point if
   they say yes — replacing it invalidates their existing authenticator
   entry).
3. Generate a secret:
   `python3 -c "import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/daemon'); import totp; print(totp.generate_secret())"`
4. Write (or replace) the `TOTP_SECRET=<value>` line in `$SQUEEZER_HOME/.env`
   (create the file from `${CLAUDE_PLUGIN_ROOT}/templates/env.example`'s
   shape first if it doesn't exist yet — but don't clobber other existing
   values in it).
5. Print the provisioning URI for manual entry:
   `python3 -c "import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/daemon'); import totp; print(totp.provisioning_uri('<the secret from step 3>'))"`
   Show the user both the raw base32 secret and this URI, and tell them to
   add it to Google Authenticator (or any TOTP app) via manual key entry —
   paste the secret in, algorithm SHA1, 6 digits, 30-second period (Google
   Authenticator's defaults already match this).
6. Ask the user to read back the current 6-digit code from their
   authenticator app, then confirm it verifies:
   `python3 -c "import sys, time; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/daemon'); import totp; print(totp.verify_code('<the secret>', '<code the user gave>', None, time.time())[0])"`
   If this prints `False`, the code was wrong or the secret was mistyped
   into the app — ask the user to re-check and try again rather than
   silently continuing.
7. Once confirmed, tell the user 2FA is active and they can now send
   `/elevate <code> <hours>` (hours: 2, 4, 8, or 24) over Telegram.
```

- [ ] **Step 3: Verify the command's underlying Python calls work**

Run (from the squeezer repo root, using a scratch `SQUEEZER_HOME` so this doesn't touch the real one):

```bash
export SQUEEZER_HOME=$(mktemp -d)
python3 -c "
import sys
sys.path.insert(0, 'daemon')
import totp, time
secret = totp.generate_secret()
print('secret:', secret)
print('uri:', totp.provisioning_uri(secret))
code = totp.totp_at_step(secret, totp.current_step(time.time()))
print('verify:', totp.verify_code(secret, code, None, time.time()))
"
rm -rf "$SQUEEZER_HOME"
```

Expected: prints a secret, a `otpauth://totp/...` URI, and `verify: (True, <some int>)` — confirming the exact call shapes used in `commands/2fa-setup.md`'s steps 3, 5, and 6 actually work end-to-end.

- [ ] **Step 4: Commit**

```bash
git add templates/env.example commands/2fa-setup.md
git commit -m "feat: add /squeezer:2fa-setup enrollment command"
```

---

### Task 9: Full suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the complete test suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS — every test in `tests/`, old and new, green. This is the repo's own documented bar ("Every new feature added to squeezer gets minimal tests under tests/ ... Run python3 -m pytest tests/ before considering such a task done" — `CLAUDE.md`).

- [ ] **Step 2: Confirm no stray debug output or scratch files**

```bash
git status
```

Expected: clean except for the commits already made in Tasks 1-8 — no leftover scratch `SQUEEZER_HOME` directories, no `__pycache__` changes staged.
