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
  3. the token file `PublicForward/ForVPS/.api_token` (written by manage_token.py)

This module has no third-party dependencies so both vps_server.py and
manage_token.py can import it.
"""
from __future__ import annotations

import hmac
import os
import secrets
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


def save_token(token: str) -> Path:
    """Persist *token* to TOKEN_FILE with owner-only permissions (0600)."""
    err = validate_token_format(token)
    if err:
        raise ValueError(err)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
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


def _read_token_file() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""


def load_token(cli_value: Optional[str] = None) -> tuple[str, str]:
    """Resolve the active API token. Returns (token, source).

    *source* is one of "cli", "env", "file" or "none" (token is "" then).
    """
    if cli_value and cli_value.strip():
        return cli_value.strip(), "cli"
    env_value = os.environ.get(ENV_VAR, "").strip()
    if env_value:
        return env_value, "env"
    file_value = _read_token_file()
    if file_value:
        return file_value, "file"
    return "", "none"


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