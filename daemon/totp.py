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
