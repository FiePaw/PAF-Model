"""
PublicForward/ForVPS/paf_auth.py — client-facing API token helpers.

One single shared token protects the whole public REST surface of the gateway
(`vps_server.py`). Clients must send it as:

    Authorization: Bearer <token>

This is SEPARATE from `PAF_TOKEN`, which only gates worker WebSocket
connections (`/ws/worker`).

Token resolution order (first non-empty wins):
  1. `--api-token` CLI flag of vps_server.py
  2. `PAF_API_TOKEN` environment variable
  3. the token file `PublicForward/ForVPS/.api_token` (written by manage_token.py
     or the live console)

Optional expiry: a token kept in the token FILE may carry an expiry (e.g. 7d).
The file is plain text, human-readable and editable:

    paf_xxxxxxxx...
    expires_at=2026-10-26T14:03:00Z      <- optional, UTC; absent = never expires

Tokens from the CLI flag / env var never expire.

This module has no third-party dependencies so both vps_server.py and
manage_token.py can import it.
"""
from __future__ import annotations

import hmac
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ENV_VAR = "PAF_API_TOKEN"
TOKEN_FILE = Path(__file__).resolve().parent / ".api_token"
TOKEN_PREFIX = "paf_"
MIN_TOKEN_LENGTH = 16


# --------------------------------------------------------------------------- #
# Generation / storage
# --------------------------------------------------------------------------- #
def generate_token() -> str:
    """Return a new cryptographically random token (`paf_<43 url-safe chars>`)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def validate_token_format(token: str) -> Optional[str]:
    """Return an error message if *token* is unacceptable, else None."""
    if not token or token != token.strip():
        return "Token must not be empty or have leading/trailing whitespace."
    if any(ch.isspace() for ch in token):
        return "Token must not contain whitespace."
    if len(token) < MIN_TOKEN_LENGTH:
        return f"Token must be at least {MIN_TOKEN_LENGTH} characters long."
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        return "Token must contain ASCII characters only."
    return None


# --------------------------------------------------------------------------- #
# Expiry helpers
# --------------------------------------------------------------------------- #
_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
NEVER_WORDS = {"never", "none", "off", "0"}
MAX_DURATION_SECONDS = 10 * 365 * 86400  # sanity cap: 10 years
_EXPIRES_KEY = "expires_at="
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
EXPIRY_HELP = ("number + unit s/m/h/d/w (e.g. 30m, 12h, 7d, 2w), "
               "or 'never' for no expiry")


def parse_duration(text: str) -> int:
    """'30m' / '12h' / '7d' / '2w' -> seconds. Raises ValueError if invalid."""
    m = _DURATION_RE.match((text or "").strip())
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"Invalid expiry '{text}'. Use {EXPIRY_HELP}.")
    seconds = int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
    if seconds > MAX_DURATION_SECONDS:
        raise ValueError("Expiry is too long (maximum is 10 years).")
    return seconds


def expiry_from_arg(text: Optional[str], now: Optional[float] = None) -> Optional[int]:
    """Expiry argument -> absolute epoch seconds, or None for 'never'/not given."""
    if text is None:
        return None
    t = text.strip().lower()
    if t in NEVER_WORDS:
        return None
    return int((time.time() if now is None else now) + parse_duration(t))


def is_expired(expires_at: Optional[float], now: Optional[float] = None) -> bool:
    if expires_at is None:
        return False
    return (time.time() if now is None else now) >= expires_at


def _humanize(seconds: float) -> str:
    s = int(max(seconds, 0))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def format_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def describe_expiry(expires_at: Optional[float], now: Optional[float] = None) -> str:
    """Human-readable expiry status."""
    if expires_at is None:
        return "never expires"
    now = time.time() if now is None else now
    if now >= expires_at:
        return f"EXPIRED at {format_timestamp(expires_at)} ({_humanize(now - expires_at)} ago)"
    # ceil: a freshly created "7d" token reads "in 7d 0h", not "in 6d 23h".
    return f"expires {format_timestamp(expires_at)} (in {_humanize(math.ceil(expires_at - now))})"


def save_token(token: str, expires_at: Optional[float] = None) -> Path:
    """Persist *token* (+ optional expiry) to TOKEN_FILE with owner-only permissions (0600)."""
    err = validate_token_format(token)
    if err:
        raise ValueError(err)
    content = token + "\n"
    if expires_at is not None:
        stamp = datetime.fromtimestamp(int(expires_at), tz=timezone.utc).strftime(_TS_FORMAT)
        content += f"{_EXPIRES_KEY}{stamp}\n"
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        os.chmod(TOKEN_FILE, 0o600)  # in case the file already existed
    except OSError:
        pass  # e.g. Windows
    return TOKEN_FILE


def delete_token_file() -> bool:
    """Delete TOKEN_FILE. Returns True if a file was removed."""
    try:
        TOKEN_FILE.unlink()
        return True
    except FileNotFoundError:
        return False


def read_token_file() -> tuple[str, Optional[int]]:
    """Read TOKEN_FILE -> (token, expires_at). ("", None) if missing/empty.

    Line 1 is the token; an optional `expires_at=<UTC ISO>` line sets the expiry.
    An expiry line that cannot be parsed is treated as ALREADY EXPIRED (fail
    closed) rather than silently turning into "never expires".
    """
    try:
        text = TOKEN_FILE.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return "", None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0].startswith(_EXPIRES_KEY):
        return "", None
    token, expires_at = lines[0], None
    for ln in lines[1:]:
        if ln.startswith(_EXPIRES_KEY):
            try:
                dt = datetime.strptime(ln[len(_EXPIRES_KEY):].strip(), _TS_FORMAT)
                expires_at = int(dt.replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                expires_at = 1  # corrupt -> expired
    return token, expires_at


def load_token_full(cli_value: Optional[str] = None) -> tuple[str, str, Optional[int]]:
    """Resolve the active API token. Returns (token, source, expires_at).

    *source* is one of "cli", "env", "file" or "none" (token is "" then).
    Only a token from the file can carry an expiry (else expires_at is None).
    """
    if cli_value and cli_value.strip():
        return cli_value.strip(), "cli", None
    env_value = os.environ.get(ENV_VAR, "").strip()
    if env_value:
        return env_value, "env", None
    file_value, expires_at = read_token_file()
    if file_value:
        return file_value, "file", expires_at
    return "", "none", None


def load_token(cli_value: Optional[str] = None) -> tuple[str, str]:
    """Like load_token_full() but without the expiry: returns (token, source)."""
    token, source, _ = load_token_full(cli_value)
    return token, source


def mask_token(token: str) -> str:
    """Return a display-safe version of *token* (never the full secret)."""
    if not token:
        return "(none)"
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"


# --------------------------------------------------------------------------- #
# Request check
# --------------------------------------------------------------------------- #
def extract_bearer(authorization_header: Optional[str]) -> Optional[str]:
    """Pull the token out of an `Authorization: Bearer <token>` header value."""
    if not authorization_header:
        return None
    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def tokens_match(provided: str, expected: str) -> bool:
    """Constant-time comparison (safe for non-ASCII input)."""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))