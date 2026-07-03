"""
scrapers/utils.py — MERGED shared helpers for PAF-Model (DeepSeek + Qwen).

Union of both original utils modules:

  From DeepSeek : get_logger, PrettyFormatter, estimate_tokens, dump_json,
                  to_json_str, cookie_editor_json_to_playwright, AuthStore
  From Qwen     : setup_logger, safe_filename, timestamped_filename, save_json,
                  load_json, discover_cookie_files, normalize_cookies,
                  extract_code_blocks, detect_file_type, save_code_files,
                  contains_any, retry_sleep

Both loggers are provided as real implementations (they format differently):
  - get_logger()   → DeepSeek-style colored+emoji console + paf_deepseek.log
  - setup_logger() → Qwen-style pretty console + scraper.log
`setup_logger` is ALSO exposed as an alias target if callers want a single name.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from config import LOG_CONFIG, OUTPUT_CONFIG


# =========================================================================== #
# Logging — DeepSeek style (get_logger)
# =========================================================================== #
_DS_LEVEL_COLORS = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[41m",  # red bg
}
_DS_RESET = "\033[0m"
_DS_LEVEL_EMOJI = {
    "DEBUG": "🔍",
    "INFO": "ℹ️ ",
    "WARNING": "⚠️ ",
    "ERROR": "❌",
    "CRITICAL": "🔥",
}


class PrettyFormatter(logging.Formatter):
    """Console formatter with optional color + emoji per level (DeepSeek)."""

    def __init__(self, use_color: bool = True, use_emoji: bool = True) -> None:
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt=LOG_CONFIG["timestamp_format"],
        )
        self.use_color = use_color
        self.use_emoji = use_emoji

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        prefix = ""
        if self.use_emoji:
            prefix = _DS_LEVEL_EMOJI.get(record.levelname, "") + " "
        if self.use_color:
            color = _DS_LEVEL_COLORS.get(record.levelname, "")
            return f"{color}{prefix}{base}{_DS_RESET}"
        return f"{prefix}{base}"


_CONFIGURED: set[str] = set()


def get_logger(name: str = "paf_deepseek") -> logging.Logger:
    """Return a configured logger (console + rotating file). Idempotent."""
    logger = logging.getLogger(name)
    if name in _CONFIGURED:
        return logger

    logger.setLevel(LOG_CONFIG.get("level", "INFO"))
    logger.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(
        PrettyFormatter(
            use_color=LOG_CONFIG.get("use_color", True),
            use_emoji=LOG_CONFIG.get("use_emoji", True),
        )
    )
    logger.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            LOG_CONFIG["file"],
            maxBytes=LOG_CONFIG.get("max_bytes", 5 * 1024 * 1024),
            backupCount=LOG_CONFIG.get("backup_count", 5),
            encoding=LOG_CONFIG.get("encoding", "utf-8"),
        )
        file_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt=LOG_CONFIG["timestamp_format"],
            )
        )
        logger.addHandler(file_handler)
    except Exception:  # pragma: no cover - file logging is best-effort
        logger.warning("Could not attach rotating file handler.")

    _CONFIGURED.add(name)
    return logger


# =========================================================================== #
# Logging — Qwen style (setup_logger)
# =========================================================================== #
def _enable_windows_ansi() -> bool:
    """Aktifkan Virtual Terminal Processing di Windows cmd/PowerShell."""
    try:
        import ctypes
        import ctypes.wintypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VT = 0x0004
        if mode.value & ENABLE_VT:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT))
    except Exception:
        return False


def _supports_color() -> bool:
    """Deteksi apakah stderr mendukung ANSI color output."""
    if not hasattr(sys.stderr, "isatty") or not sys.stderr.isatty():
        return False
    if sys.platform == "win32":
        return _enable_windows_ansi()
    return True


_COLOR_ON = _supports_color()


def _c(code: str) -> str:
    """Return ANSI code jika warna didukung, string kosong jika tidak."""
    return code if _COLOR_ON else ""


_RESET   = _c("\033[0m")
_BOLD    = _c("\033[1m")
_DIM     = _c("\033[2m")
_RED     = _c("\033[91m")
_GREEN   = _c("\033[92m")
_YELLOW  = _c("\033[93m")
_BLUE    = _c("\033[94m")
_MAGENTA = _c("\033[95m")
_CYAN    = _c("\033[96m")
_WHITE   = _c("\033[37m")
_BG_RED  = _c("\033[41m")

_LEVEL_STYLES: dict[int, tuple[str, str]] = {
    logging.DEBUG:    (_DIM + _CYAN,            "DEBUG  "),
    logging.INFO:     (_GREEN,                  "INFO   "),
    logging.WARNING:  (_YELLOW,                 "WARN   "),
    logging.ERROR:    (_RED,                    "ERROR  "),
    logging.CRITICAL: (_BOLD + _BG_RED + _WHITE, "CRIT   "),
}

_HIGHLIGHTS: list[tuple[str, str]] = [
    ("✅",        f"{_GREEN}✅{_RESET}"),
    ("❌",        f"{_RED}❌{_RESET}"),
    ("🔌",        f"{_CYAN}🔌{_RESET}"),
    ("🔄",        f"{_YELLOW}🔄{_RESET}"),
    ("Pool ready", f"{_GREEN}{_BOLD}Pool ready{_RESET}"),
    ("Terhubung",  f"{_GREEN}Terhubung{_RESET}"),
    ("Warming up", f"{_CYAN}Warming up{_RESET}"),
    ("CONTINUE",   f"{_MAGENTA}{_BOLD}CONTINUE{_RESET}"),
    ("NEW",        f"{_CYAN}{_BOLD}NEW{_RESET}"),
    ("Gagal",      f"{_RED}Gagal{_RESET}"),
    ("Error",      f"{_RED}Error{_RESET}"),
    ("error",      f"{_RED}error{_RESET}"),
    ("Timeout",    f"{_RED}Timeout{_RESET}"),
    ("Reconnect",  f"{_YELLOW}Reconnect{_RESET}"),
    ("reconnect",  f"{_YELLOW}reconnect{_RESET}"),
    ("Konek",      f"{_CYAN}Konek{_RESET}"),
    ("Menutup",    f"{_YELLOW}Menutup{_RESET}"),
    ("dihentikan", f"{_YELLOW}dihentikan{_RESET}"),
    ("idle=",      f"{_GREEN}idle={_RESET}"),
    ("busy=",      f"{_YELLOW}busy={_RESET}"),
    ("dead=",      f"{_RED}dead={_RESET}"),
    ("starting=",  f"{_CYAN}starting={_RESET}"),
    ("total=",     f"{_WHITE}total={_RESET}"),
]


def _colorize(msg: str) -> str:
    msg = re.sub(r"(Worker#\d+)", lambda m: f"{_BLUE}{_BOLD}{m.group(1)}{_RESET}", msg)
    msg = re.sub(r"(\[[0-9a-f]{6,}\])", lambda m: f"{_YELLOW}{m.group(1)}{_RESET}", msg)
    for kw, colored in _HIGHLIGHTS:
        if kw in msg:
            msg = msg.replace(kw, colored, 1)
    return msg


class _PrettyConsoleFormatter(logging.Formatter):
    """Colorful, compact formatter for console output only (Qwen)."""
    _SEP = f"{_DIM}│{_RESET}"

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        ts = self.formatTime(record, "%H:%M:%S")
        color, label = _LEVEL_STYLES.get(record.levelno, (_WHITE, f"{record.levelname:<7}"))
        name = record.name[:14]
        msg  = _colorize(record.getMessage())
        if record.exc_info:
            exc = self.formatException(record.exc_info)
            msg += "\n" + "\n".join(f"  {_RED}{l}{_RESET}" for l in exc.splitlines())
        return (
            f"{_DIM}{ts}{_RESET}  "
            f"{color}{label}{_RESET}  "
            f"{_DIM}{name:<14}{_RESET}  "
            f"{self._SEP}  {msg}"
        )


def setup_logger(name: str) -> logging.Logger:
    """Create a named logger with both console and rotating file handlers (Qwen)."""
    logger = logging.getLogger(name)
    if logger.handlers:          # avoid duplicate handlers on re-import
        return logger

    logger.setLevel(LOG_CONFIG["level"])

    # Plain formatter untuk file (tanpa ANSI agar log file tetap bersih)
    plain_formatter = logging.Formatter(
        fmt=LOG_CONFIG["format"],
        datefmt=LOG_CONFIG["date_format"],
    )

    # Console handler — pakai pretty formatter berwarna
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(_PrettyConsoleFormatter())
    logger.addHandler(ch)

    # File handler (rotating) — tetap plain text
    fh = RotatingFileHandler(
        LOG_CONFIG["log_file"],
        maxBytes=LOG_CONFIG["max_bytes"],
        backupCount=LOG_CONFIG["backup_count"],
        encoding="utf-8",
    )
    fh.setFormatter(plain_formatter)
    logger.addHandler(fh)

    return logger


# =========================================================================== #
# Token counter (rough estimate for OpenAI-compatible usage fields)
# =========================================================================== #
def estimate_tokens(text: str) -> int:
    """Very rough token estimate. ~4 chars per token, with a word-count floor."""
    if not text:
        return 0
    by_chars = len(text) / 4
    by_words = len(text.split())
    return max(1, int(max(by_chars, by_words)))


# =========================================================================== #
# JSON helpers
# =========================================================================== #
def dump_json(data: Any, path: str | Path) -> Path:
    """Write `data` as pretty JSON using config indent/encoding. Returns path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding=OUTPUT_CONFIG["encoding"]) as f:
        json.dump(data, f, indent=OUTPUT_CONFIG["json_indent"], ensure_ascii=False)
    return p


def to_json_str(data: Any) -> str:
    return json.dumps(data, indent=OUTPUT_CONFIG["json_indent"], ensure_ascii=False)


def save_json(data: Any, path: Path | str, indent: int = OUTPUT_CONFIG["json_indent"]) -> None:
    """Serialise *data* to a UTF-8 JSON file, creating parent dirs as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=OUTPUT_CONFIG["encoding"]) as fh:
        json.dump(data, fh, indent=indent, ensure_ascii=False)


def load_json(path: Path | str) -> Any:
    """Load and return a JSON file; raises FileNotFoundError if missing."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with path.open("r", encoding=OUTPUT_CONFIG["encoding"]) as fh:
        return json.load(fh)


# =========================================================================== #
# File-name helpers (Qwen)
# =========================================================================== #
def safe_filename(text: str, max_len: int = OUTPUT_CONFIG["max_filename_length"]) -> str:
    """Convert arbitrary text into a safe filename fragment."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len]


def timestamped_filename(prefix: str, ext: str = "json") -> str:
    """Return a filename like  prefix_20240524_153012.json ."""
    ts = datetime.now().strftime(OUTPUT_CONFIG["timestamp_format"])
    return f"{prefix}_{ts}.{ext}"


# =========================================================================== #
# Cookie helpers
# =========================================================================== #
def discover_cookie_files(cookies_dir: Path) -> list[Path]:
    """Return all .json files inside *cookies_dir*, sorted by name."""
    files = sorted(Path(cookies_dir).glob("*.json"))
    return files


def normalize_cookies(raw: list[dict]) -> list[dict]:
    """
    Normalise cookies exported by Cookie-Editor extension (Qwen variant).
    Playwright expects: name, value, domain, path, secure, httpOnly, sameSite.
    """
    normalized = []
    for c in raw:
        cookie: dict[str, Any] = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "secure": c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
        }
        same_site = c.get("sameSite", "Lax")
        if isinstance(same_site, str):
            same_site = same_site.capitalize()
        if same_site not in ("Strict", "Lax", "None"):
            same_site = "Lax"
        cookie["sameSite"] = same_site

        for key in ("expires", "expirationDate", "expiry"):
            if key in c and isinstance(c[key], (int, float)):
                cookie["expires"] = int(c[key])
                break

        normalized.append(cookie)
    return normalized


def cookie_editor_json_to_playwright(raw_cookies: list[dict]) -> list[dict]:
    """
    Convert a Cookie-Editor export into the schema accepted by Playwright's
    `context.add_cookies()` (DeepSeek variant — session-cookie aware).
    """
    converted: list[dict] = []
    for c in raw_cookies:
        name = c.get("name")
        value = c.get("value")
        if name is None or value is None:
            continue

        domain = c.get("domain", "")
        host_only = bool(c.get("hostOnly", False))
        if host_only and domain.startswith("."):
            domain = domain.lstrip(".")

        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": _normalize_same_site(c.get("sameSite")),
        }

        is_session = bool(c.get("session", False))
        exp = c.get("expirationDate")
        if not is_session and exp is not None:
            try:
                cookie["expires"] = float(exp)
            except (TypeError, ValueError):
                pass

        converted.append(cookie)

    return converted


def _normalize_same_site(value: Any) -> str:
    """Map Cookie-Editor sameSite (incl. literal null) to Playwright's enum."""
    if value is None:
        return "Lax"
    v = str(value).strip().lower()
    if v in ("strict",):
        return "Strict"
    if v in ("none", "no_restriction", "unspecified"):
        return "None"
    return "Lax"


# =========================================================================== #
# Code-block extraction (Qwen)
# =========================================================================== #
LANG_EXTENSIONS: dict[str, str] = {
    "python": "py", "py": "py", "javascript": "js", "js": "js",
    "typescript": "ts", "ts": "ts", "bash": "sh", "sh": "sh", "shell": "sh",
    "html": "html", "css": "css", "json": "json", "yaml": "yaml", "yml": "yaml",
    "sql": "sql", "java": "java", "cpp": "cpp", "c": "c", "go": "go",
    "rust": "rs", "php": "php", "ruby": "rb", "swift": "swift", "kotlin": "kt",
    "r": "r", "markdown": "md", "md": "md", "xml": "xml",
    "dockerfile": "dockerfile", "toml": "toml", "ini": "ini",
}

CODE_BLOCK_RE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+-]*)\n(?P<code>.*?)```",
    re.DOTALL,
)


def extract_code_blocks(text: str) -> list[dict]:
    """Parse all fenced code blocks from *text* → list of {index,lang,extension,code}."""
    blocks = []
    for idx, match in enumerate(CODE_BLOCK_RE.finditer(text), start=1):
        lang = match.group("lang").strip().lower() or "text"
        ext = LANG_EXTENSIONS.get(lang, "txt")
        blocks.append({
            "index": idx,
            "lang": lang,
            "extension": ext,
            "code": match.group("code"),
        })
    return blocks


def detect_file_type(content: str) -> str:
    """Heuristic: guess the primary file type present in *content*."""
    m = CODE_BLOCK_RE.search(content)
    if m and m.group("lang"):
        return m.group("lang").lower()
    if re.search(r"def |import |from .+ import |class .+:", content):
        return "python"
    if re.search(r"function |const |let |var |=>", content):
        return "javascript"
    if re.search(r"<html|<!DOCTYPE", content, re.IGNORECASE):
        return "html"
    if re.search(r"\$\w+\s*=|echo ", content):
        return "bash"
    return "text"


def save_code_files(blocks: list[dict], output_dir: Path, prefix: str = "snippet") -> list[Path]:
    """Write each code block to its own file inside *output_dir*."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for block in blocks:
        filename = f"{prefix}_{block['index']:02d}.{block['extension']}"
        path = output_dir / filename
        path.write_text(block["code"], encoding="utf-8")
        saved.append(path)
    return saved


# =========================================================================== #
# Misc (Qwen)
# =========================================================================== #
def contains_any(text: str, phrases: list[str]) -> bool:
    """Return True if *text* (case-insensitive) contains any of *phrases*."""
    text_lower = text.lower()
    return any(p.lower() in text_lower for p in phrases)


def retry_sleep(seconds: float) -> None:
    """Block for *seconds*; used between retries / rotations."""
    time.sleep(seconds)


# --------------------------------------------------------------------------- #
# AuthStore — single credentials file (cookies/auth.json) for all accounts
# (DeepSeek)
# --------------------------------------------------------------------------- #
class AuthStore:
    """
    Loads ALL account credentials from a single file: cookies/auth.json.

    Accepted formats (all auto-detected):
      1. List of accounts: [{"name","email","password"}, ...]
      2. Object with "accounts": {"accounts": [...]}
      3. Single account object: {"email","password"}  -> name "account1"
      4. Mapping name -> credentials: {"account1": {"email","password"}, ...}
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._accounts: dict[str, dict[str, str]] = {}
        self._order: list[str] = []
        self._load()

    def _add(self, name: Optional[str], email: Optional[str],
             password: Optional[str]) -> None:
        name = name or f"account{len(self._order) + 1}"
        if name in self._accounts:
            return
        self._accounts[name] = {"email": email, "password": password}
        self._order.append(name)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        if isinstance(data, list):
            for a in data:
                if isinstance(a, dict):
                    self._add(a.get("name"), a.get("email"), a.get("password"))
        elif isinstance(data, dict) and isinstance(data.get("accounts"), list):
            for a in data["accounts"]:
                if isinstance(a, dict):
                    self._add(a.get("name"), a.get("email"), a.get("password"))
        elif isinstance(data, dict) and ("email" in data or "password" in data):
            self._add(data.get("name"), data.get("email"), data.get("password"))
        elif isinstance(data, dict):
            for name, creds in data.items():
                if isinstance(creds, dict):
                    self._add(name, creds.get("email"), creds.get("password"))

    def account_names(self) -> list[str]:
        return list(self._order)

    def get(self, name: Optional[str]) -> Optional[dict[str, str]]:
        if name is None:
            return None
        return self._accounts.get(name)

    def first(self) -> Optional[str]:
        return self._order[0] if self._order else None

    def __len__(self) -> int:
        return len(self._order)
