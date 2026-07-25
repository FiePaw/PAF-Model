"""
config package — unified configuration for PAF-Model (DeepSeek + Qwen).

Re-exports every name from the sub-modules so that legacy imports keep working
unchanged, e.g.:

    from config import DEEPSEEK_CONFIG, QWEN_CONFIG, BROWSER_CONFIG, COOKIES_DIR

Layout:
    config/common.py    → shared paths, browser, rotation, output, logging
    config/deepseek.py  → DEEPSEEK_CONFIG, AUTH_CONFIG, JSON_API_CONFIG
    config/qwen.py      → QWEN_CONFIG
"""
from __future__ import annotations

from config.common import (
    BASE_DIR,
    COOKIES_DIR,
    OUTPUT_DIR,
    CODE_OUTPUT_DIR,
    LOGS_DIR,
    PROFILES_DIR,
    DATA_SESSION_DIR,
    DEBUG_DIR,
    USER_AGENT,
    BROWSER_CONFIG,
    CHROMIUM_LAUNCH_ARGS,
    QWEN_LAUNCH_ARGS,
    PERSISTENT_CONTEXT_CONFIG,
    ROTATION_CONFIG,
    OUTPUT_CONFIG,
    LOG_CONFIG,
)
from config.deepseek import (
    DEEPSEEK_CONFIG,
    AUTH_CONFIG,
    JSON_API_CONFIG,
)
from config.qwen import QWEN_CONFIG, QWEN_AUTH_CONFIG

__all__ = [
    # paths
    "BASE_DIR",
    "COOKIES_DIR",
    "OUTPUT_DIR",
    "CODE_OUTPUT_DIR",
    "LOGS_DIR",
    "PROFILES_DIR",
    "DATA_SESSION_DIR",
    "DEBUG_DIR",
    # browser
    "USER_AGENT",
    "BROWSER_CONFIG",
    "CHROMIUM_LAUNCH_ARGS",
    "QWEN_LAUNCH_ARGS",
    "PERSISTENT_CONTEXT_CONFIG",
    # rotation / output / logging
    "ROTATION_CONFIG",
    "OUTPUT_CONFIG",
    "LOG_CONFIG",
    # deepseek
    "DEEPSEEK_CONFIG",
    "AUTH_CONFIG",
    "JSON_API_CONFIG",
    # qwen
    "QWEN_CONFIG",
    "QWEN_AUTH_CONFIG",
]
