"""
config/qwen.py — Qwen-specific configuration for PAF-Model.

Holds QWEN_CONFIG (base_url, selectors, timeouts, think-mode labels).
Unchanged from the original standalone Qwen repo.
"""
from __future__ import annotations

from config.common import COOKIES_DIR, PROFILES_DIR  # noqa: F401


# --------------------------------------------------------------------------- #
# Qwen AI Settings
# --------------------------------------------------------------------------- #
QWEN_CONFIG = {
    "base_url": "https://chat.qwen.ai",
    "new_chat_url": "https://chat.qwen.ai",
    "selectors": {
        "prompt_textarea": 'textarea[placeholder], div[contenteditable="true"], #chat-input',
        "send_button": 'button[type="submit"], button[aria-label*="Send"], button[aria-label*="send"]',
        "response_container": '.message-content, .chat-message, [class*="response"], [class*="message"]',
        "loading_indicator": '[class*="loading"], [class*="spinner"], [class*="typing"]',
        "stop_button": 'button[aria-label*="Stop"], button[title*="Stop"]',
        "new_chat_button": 'button[aria-label*="New chat"], a[href*="new"], [class*="new-chat"]',

        # Think mode dropdown
        "think_mode_trigger": ".qwen-select-thinking-label",
        "think_mode_selected": ".qwen-select-option-selected-label-container",
        "think_mode_options": ".rc-virtual-list-holder-inner",
    },
    "timeouts": {
        "page_load": 10_000,
        "response_wait": 300_000,
        "stability_check": 1_000,
        "between_actions": 800,
    },

    # ── Think mode ────────────────────────────────────────────────────────────
    # Valid values: "auto" | "thinking" | "fast"
    # This is the global default; can be overridden per-request via the
    # think_mode argument of send_prompt() / scrape().
    "default_think_mode": "fast",

    # Label text as it appears in the Qwen dropdown (case-insensitive match)
    "think_mode_labels": {
        "auto":     "auto",
        "thinking": "thinking",
        "fast":     "fast",
    },
}

