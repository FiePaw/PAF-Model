"""
PublicForward/ForVPS/paf_auth.py — multi-client API token management.

Supports multiple simultaneous API tokens for clients of the gateway
(`vps_server.py`). Clients present their token via:

    Authorization: Bearer <token>

Each token can have:
  - A friendly name / label (e.g. 'alice', 'mobile-app', 'test-client')
  - An individual expiry (e.g. 30m, 12h, 7d, 2w, or never)
  - An independent lifecycle (revoke or expire one token without affecting others)

This is SEPARATE from `PAF_TOKEN`, which only gates worker WebSocket
connections (`/ws/worker`).

Tokens can come from:
  1. The tokens file: `PublicForward/ForVPS/.api_tokens.json` (managed via console or manage_token.py)
  2. Legacy token file: `.api_token` (automatically migrated if present)
  3. `PAF_API_TOKEN` environment variable (single or comma-separated `name:token` or bare `token`)
  4. `--api-token` CLI flag of vps_server.py
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ENV_VAR = "PAF_API_TOKEN"
TOKENS_FILE = Path(__file__).resolve().parent / ".api_tokens.json"
LEGACY_TOKEN_FILE = Path(__file__).resolve().parent / ".api_token"
TOKEN_FILE = TOKENS_FILE  # export alias
TOKEN_PREFIX = "paf_"
MIN_TOKEN_LENGTH = 16

NAME_RE = re.compile(r"^[a-zA-Z0-9_\.-]{1,64}$")


@dataclass
class TokenInfo:
    token: str
    name: str
    created_at: float
    expires_at: Optional[float] = None
    source: str = "file"  # "file", "env", "cli"

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.time() if now is None else now) >= self.expires_at

    def describe(self, now: Optional[float] = None) -> str:
        return describe_expiry(self.expires_at, now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "source": self.source,
        }


# --------------------------------------------------------------------------- #
# Validation & Generation
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


def validate_name(name: str) -> Optional[str]:
    """Validate token name/label."""
    if not name or not name.strip():
        return "Token name cannot be empty."
    clean = name.strip()
    if not NAME_RE.match(clean):
        return "Name must be 1-64 alphanumeric characters, hyphens, underscores, or dots."
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
    return f"expires {format_timestamp(expires_at)} (in {_humanize(math.ceil(expires_at - now))})"


# --------------------------------------------------------------------------- #
# Storage & Migration (.api_tokens.json)
# --------------------------------------------------------------------------- #
def _migrate_legacy_file_if_needed() -> dict[str, TokenInfo]:
    """If .api_token exists and .api_tokens.json does not, migrate seamlessly."""
    if TOKENS_FILE.exists() or not LEGACY_TOKEN_FILE.exists():
        return {}
    try:
        text = LEGACY_TOKEN_FILE.read_text(encoding="utf-8")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines or lines[0].startswith(_EXPIRES_KEY):
            return {}
        token, expires_at = lines[0], None
        for ln in lines[1:]:
            if ln.startswith(_EXPIRES_KEY):
                try:
                    dt = datetime.strptime(ln[len(_EXPIRES_KEY):].strip(), _TS_FORMAT)
                    expires_at = int(dt.replace(tzinfo=timezone.utc).timestamp())
                except ValueError:
                    expires_at = 1
        info = TokenInfo(
            token=token,
            name="default",
            created_at=time.time(),
            expires_at=expires_at,
            source="file",
        )
        tokens = {token: info}
        save_tokens_file(tokens)
        return tokens
    except Exception as exc:
        print(f"[paf_auth] Legacy token migration warning: {exc}")
        return {}


def read_tokens_file() -> dict[str, TokenInfo]:
    """Read all tokens from TOKENS_FILE (with migration fallback)."""
    migrated = _migrate_legacy_file_if_needed()
    if migrated:
        return migrated

    if not TOKENS_FILE.exists():
        return {}

    try:
        data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        result: dict[str, TokenInfo] = {}
        for tok, meta in data.items():
            if not isinstance(meta, dict):
                continue
            name = meta.get("name") or "unnamed"
            created_at = float(meta.get("created_at") or time.time())
            expires_at = meta.get("expires_at")
            if expires_at is not None:
                expires_at = float(expires_at)
            result[tok] = TokenInfo(
                token=tok,
                name=name,
                created_at=created_at,
                expires_at=expires_at,
                source="file",
            )
        return result
    except Exception as exc:
        print(f"[paf_auth] Error reading tokens file: {exc}")
        return {}


def save_tokens_file(tokens: dict[str, TokenInfo]) -> Path:
    """Save tokens to TOKENS_FILE with owner-only permissions."""
    raw = {}
    for tok, info in tokens.items():
        if info.source == "file":
            raw[tok] = info.to_dict()

    payload = json.dumps(raw, indent=2) + "\n"
    fd = os.open(TOKENS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    try:
        os.chmod(TOKENS_FILE, 0o600)
    except OSError:
        pass
    return TOKENS_FILE


def delete_token_file() -> bool:
    """Delete token files. Returns True if any file was removed."""
    removed = False
    for p in (TOKENS_FILE, LEGACY_TOKEN_FILE):
        try:
            p.unlink()
            removed = True
        except FileNotFoundError:
            pass
    return removed


# --------------------------------------------------------------------------- #
# High-Level Token Management (CRUD)
# --------------------------------------------------------------------------- #
def load_all_tokens(cli_value: Optional[str] = None) -> dict[str, TokenInfo]:
    """Load tokens from file, environment variable, and CLI flag."""
    tokens = read_tokens_file()

    # Environment variable tokens (PAF_API_TOKEN)
    env_raw = os.environ.get(ENV_VAR, "").strip()
    if env_raw:
        # Supports comma-separated list of tokens or 'name:token' pairs
        parts = [p.strip() for p in env_raw.split(",") if p.strip()]
        for idx, part in enumerate(parts, 1):
            if ":" in part:
                name, tok = part.split(":", 1)
                name, tok = name.strip(), tok.strip()
            else:
                name, tok = (f"env-{idx}" if len(parts) > 1 else "env"), part
            if tok and not validate_token_format(tok):
                tokens[tok] = TokenInfo(
                    token=tok,
                    name=name,
                    created_at=time.time(),
                    expires_at=None,
                    source="env",
                )

    # CLI flag token
    if cli_value and cli_value.strip():
        cli_tok = cli_value.strip()
        tokens[cli_tok] = TokenInfo(
            token=cli_tok,
            name="cli",
            created_at=time.time(),
            expires_at=None,
            source="cli",
        )

    return tokens


def find_token_by_name(name: str, tokens: dict[str, TokenInfo]) -> Optional[TokenInfo]:
    """Search for a token by its case-insensitive name/label."""
    target = name.strip().lower()
    for info in tokens.values():
        if info.name.lower() == target:
            return info
    return None


def create_token(
    name: str,
    expiry_text: Optional[str] = None,
    token_value: Optional[str] = None,
) -> tuple[TokenInfo, Path]:
    """Create and persist a new token with given name and optional expiry."""
    name_err = validate_name(name)
    if name_err:
        raise ValueError(name_err)

    clean_name = name.strip()
    tokens = read_tokens_file()

    if find_token_by_name(clean_name, tokens):
        raise ValueError(f"A token with name '{clean_name}' already exists.")

    tok = (token_value or generate_token()).strip()
    tok_err = validate_token_format(tok)
    if tok_err:
        raise ValueError(tok_err)
    if tok in tokens:
        raise ValueError("This token value is already assigned to another entry.")

    expires_at = expiry_from_arg(expiry_text) if expiry_text else None
    info = TokenInfo(
        token=tok,
        name=clean_name,
        created_at=time.time(),
        expires_at=expires_at,
        source="file",
    )
    tokens[tok] = info
    path = save_tokens_file(tokens)
    return info, path


def revoke_token(identifier: str) -> Optional[TokenInfo]:
    """Revoke/delete a token by its name or exact token string."""
    ident = identifier.strip()
    tokens = read_tokens_file()
    found_tok = None

    for tok, info in tokens.items():
        if info.name.lower() == ident.lower() or tok == ident:
            found_tok = tok
            break

    if not found_tok:
        return None

    removed_info = tokens.pop(found_tok)
    save_tokens_file(tokens)
    return removed_info


def update_token_expiry(identifier: str, expiry_text: str) -> Optional[TokenInfo]:
    """Update expiry for a token identified by name or token string."""
    ident = identifier.strip()
    tokens = read_tokens_file()
    target_info: Optional[TokenInfo] = None

    for tok, info in tokens.items():
        if info.name.lower() == ident.lower() or tok == ident:
            target_info = info
            break

    if not target_info:
        return None

    target_info.expires_at = expiry_from_arg(expiry_text)
    save_tokens_file(tokens)
    return target_info


# --------------------------------------------------------------------------- #
# Request Verification
# --------------------------------------------------------------------------- #
def mask_token(token: str) -> str:
    """Return a display-safe version of *token* (never the full secret)."""
    if not token:
        return "(none)"
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"


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


def find_matching_token(
    provided: str, tokens: dict[str, TokenInfo]
) -> Optional[TokenInfo]:
    """Look up provided token in constant-time across active tokens."""
    matched: Optional[TokenInfo] = None
    for tok, info in tokens.items():
        if tokens_match(provided, tok):
            matched = info
    return matched


# --------------------------------------------------------------------------- #
# Legacy Compatibility
# --------------------------------------------------------------------------- #
def read_token_file() -> tuple[str, Optional[int]]:
    """Legacy helper: returns (first_token, expires_at) or ('', None)."""
    tokens = read_tokens_file()
    if not tokens:
        return "", None
    first = next(iter(tokens.values()))
    return first.token, int(first.expires_at) if first.expires_at else None


def load_token_full(
    cli_value: Optional[str] = None,
) -> tuple[str, str, Optional[int]]:
    """Legacy helper: returns (first_token, source, expires_at)."""
    tokens = load_all_tokens(cli_value)
    if not tokens:
        return "", "none", None
    for tok, info in tokens.items():
        if not info.is_expired():
            return tok, info.source, int(info.expires_at) if info.expires_at else None
    first = next(iter(tokens.values()))
    return first.token, first.source, int(first.expires_at) if first.expires_at else None


def load_token(cli_value: Optional[str] = None) -> tuple[str, str]:
    token, source, _ = load_token_full(cli_value)
    return token, source


def save_token(token: str, expires_at: Optional[float] = None) -> Path:
    """Legacy helper: save token under name 'default'."""
    tokens = read_tokens_file()
    info = TokenInfo(
        token=token,
        name="default",
        created_at=time.time(),
        expires_at=expires_at,
        source="file",
    )
    tokens[token] = info
    return save_tokens_file(tokens)
