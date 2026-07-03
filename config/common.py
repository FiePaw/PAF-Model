"""
config/common.py — Shared configuration for PAF-Model (DeepSeek + Qwen).

Contains everything that is IDENTICAL (or unified) across both backends:
  - Base directory paths (auto-created on import)
  - BROWSER_CONFIG, PERSISTENT_CONTEXT_CONFIG, CHROMIUM_LAUNCH_ARGS
  - ROTATION_CONFIG (merged: Qwen's granular split + DeepSeek's flat list + session_ttl)
  - OUTPUT_CONFIG, LOG_CONFIG (super-set of keys read by both utils modules)

Backend-specific config lives in config/deepseek.py and config/qwen.py.
Everything is re-exported from config/__init__.py so legacy imports like
`from config import BROWSER_CONFIG` keep working unchanged.
"""
from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR: Path = Path(__file__).resolve().parent.parent  # repo root (config/ is a pkg)

COOKIES_DIR: Path = BASE_DIR / "cookies"
OUTPUT_DIR: Path = BASE_DIR / "output"
CODE_OUTPUT_DIR: Path = OUTPUT_DIR / "code"
LOGS_DIR: Path = BASE_DIR / "logs"
PROFILES_DIR: Path = BASE_DIR / "profiles"
DATA_SESSION_DIR: Path = BASE_DIR / "dataSession"
DEBUG_DIR: Path = BASE_DIR / "debug"

# Create every directory automatically on import.
for _d in (
    COOKIES_DIR,
    OUTPUT_DIR,
    CODE_OUTPUT_DIR,
    LOGS_DIR,
    PROFILES_DIR,
    DATA_SESSION_DIR,
    DEBUG_DIR,
):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #
# A recent Chrome desktop user agent. Update periodically so it matches a real
# Chrome build (anti-bot systems flag stale UAs).
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BROWSER_CONFIG: dict = {
    "headless": True,
    "slow_mo": 0,
    "viewport": {"width": 1280, "height": 800},
    "user_agent": USER_AGENT,
    "locale": "en-US",
    "timezone_id": "Asia/Jakarta",
    # Per-character typing delay (ms). 0 = key events still dispatched per
    # character (so site input handlers fire) but without artificial delay.
    # Read by the DeepSeek scraper; harmless extra key for the Qwen scraper.
    "type_delay_ms": 0,
}

# Extra Chromium launch args used to reduce automation fingerprinting.
# DeepSeek uses the fuller set (via PERSISTENT_CONTEXT_CONFIG["launch_args"]);
# Qwen uses its original smaller set (via PERSISTENT_CONTEXT_CONFIG["args"]).
CHROMIUM_LAUNCH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--start-maximized",
]

# Qwen's original (smaller) launch-arg set, preserved verbatim.
QWEN_LAUNCH_ARGS: list[str] = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
]

PERSISTENT_CONTEXT_CONFIG: dict = {
    # When True the scraper uses launch_persistent_context() so cookies /
    # local storage / service workers persist on disk per-account.
    "enabled": True,
    "default_profile": "default",
    # DeepSeek base reads "launch_args"; Qwen base reads "args".
    "launch_args": CHROMIUM_LAUNCH_ARGS,
    "args": QWEN_LAUNCH_ARGS,
}


# --------------------------------------------------------------------------- #
# Account rotation — MERGED
# --------------------------------------------------------------------------- #
# Qwen split rate-limit phrases into "restart-first" vs "rotate-now"; DeepSeek
# used a single flat "rate_limit_phrases" list plus "rotate_immediately_phrases".
# We keep BOTH structures so each backend reads the key it expects. All phrase
# lists are super-sets (unions) — safe because backend-specific copy from the
# other service never appears in a given backend's page.
ROTATION_CONFIG: dict = {
    # (Qwen) restart browser first → retry → then rotate account.
    "rate_limit_restart_first_phrases": [
        "allocated quota exceeded",
        "increase your quota limit",
        "token limit",
        "quota exceeded",
        "usage limit",
        "daily limit",
    ],
    # (Qwen) rotate account immediately without restarting the browser.
    "rate_limit_rotate_phrases": [
        "rate limit",
        "too many requests",
        "please try again later",
        "request limit",
        "you've reached",
    ],
    # (DeepSeek) rotate immediately (no restart first).
    "rotate_immediately_phrases": [
        "you've reached your usage limit",
        "daily limit reached",
        "quota exceeded",
    ],
    # Flat union used by is_rate_limited() for generic detection (both backends).
    "rate_limit_phrases": [
        # DeepSeek
        "server busy",
        "server is busy",
        "please try again later",
        "you've reached your",
        "you have reached your",
        "usage limit",
        "rate limit",
        "too many requests",
        "请稍后再试",
        "服务器繁忙",
        # Qwen / Alibaba
        "quota exceeded",
        "you've reached",
        "daily limit",
        "request limit",
        "allocated quota exceeded",
        "increase your quota limit",
        "token limit",
    ],
    # Session expired / logged out markers (union).
    "session_expired_phrases": [
        "log in",
        "sign in",
        "session expired",
        "please log in again",
        "please log in",
        "sign in to continue",
        "unauthorized",
        "login required",
        "登录",
    ],
    # Page-crash markers (union of Chromium error pages + Qwen fatal errors).
    "page_crash_phrases": [
        "aw, snap",
        "page crashed",
        "he's dead, jim",
        "out of memory",
        "oops! something unexpected happened",
        "something unexpected happened",
        "failure code:",
        "try refreshing",
        "oops! there was an issue connecting",
    ],
    "max_retries_per_account": 2,
    "retry_delay": 2.0,
    "rotation_delay": 3.0,
    "max_browser_restarts": 3,
    "browser_restart_delay": 5.0,
    # (DeepSeek) how long a session stays alive after its last use (seconds).
    "session_ttl": 3600,
}


# --------------------------------------------------------------------------- #
# Output / logging — MERGED (super-set of keys both utils modules read)
# --------------------------------------------------------------------------- #
OUTPUT_CONFIG: dict = {
    "json_indent": 2,
    "encoding": "utf-8",
    "timestamp_format": "%Y%m%d_%H%M%S",
    # DeepSeek
    "save_code_blocks": True,
    # Qwen
    "max_filename_length": 50,
}

LOG_CONFIG: dict = {
    "level": "INFO",
    "encoding": "utf-8",
    # DeepSeek get_logger reads these:
    "timestamp_format": "%Y-%m-%d %H:%M:%S",
    "file": str(LOGS_DIR / "paf_deepseek.log"),
    "use_color": True,
    "use_emoji": True,
    # Qwen setup_logger reads these:
    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    "date_format": "%Y-%m-%d %H:%M:%S",
    "log_file": str(LOGS_DIR / "scraper.log"),
    # Shared rotation settings:
    "max_bytes": 10 * 1024 * 1024,
    "backup_count": 5,
}
