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
