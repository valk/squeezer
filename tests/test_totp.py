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
