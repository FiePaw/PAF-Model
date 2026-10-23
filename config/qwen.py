"""
config/qwen.py — Qwen-specific configuration for PAF-Model.

Holds QWEN_CONFIG (base_url, selectors, timeouts, think-mode labels)
and AUTH_CONFIG (email+password / persistent-profile auth, mirroring
the DeepSeek auth model).
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
        "prompt_textarea": 'textarea.message-input-textarea, textarea[placeholder], div[contenteditable="true"], #chat-input',
        "send_button": 'button.send-button:not(.disabled), button[aria-label="Send"]:not([disabled]), button[type="submit"], button[aria-label*="Send"]',
        "response_container": '.qwen-markdown, .qwen-markdown-loose, .chat-response-message, [class*="response-message"], [class*="response"], [class*="message"]',
        "loading_indicator": '[class*="loading"], [class*="spinner"], [class*="typing"], [class*="thinking"]',
        "stop_button": 'button.stop-btn, button[aria-label*="Stop"], button[title*="Stop"], button[aria-label*="stop" i]',
        "new_chat_button": '[aria-label="New Chat"], div.sidebar-entry-fixed-list-content, button[aria-label*="New chat"], a[href*="new"], [class*="new-chat"]',

        # Think mode dropdown (Qwen v2 UI + legacy fallbacks)
        "think_mode_trigger": ".qwen-thinking-selector, .qwen-chat-v2-dropdown-menu-trigger, .qwen-select-thinking-label",
        "think_mode_selected": ".qwen-chat-v2-dropdown-menu-select-label, .qwen-thinking-selector, .qwen-select-option-selected-label-container",
        "think_mode_options": ".qwen-chat-v2-dropdown-menu li, .qwen-chat-v2-dropdown-menu-item, .rc-virtual-list-holder-inner",

        # ── Login / auth selectors (for email+password login) ─────────────
        # Selectors untuk mendeteksi tombol Login / Sign Up di halaman utama
        "login_button":  [
            'button:has-text("Log in")',
            'button:has-text("Login")',
            'a:has-text("Log in")',
            'a:has-text("Login")',
            '[class*="login"]:not(input)',
        ],
        "signup_button": [
            'button:has-text("Sign up")',
            'button:has-text("Sign Up")',
            'a:has-text("Sign up")',
            'a:has-text("Sign Up")',
        ],
        # Form login di https://chat.qwen.ai/auth
        "login": {
            "switch_password_button": [
                'button:has-text("Log in with a password")',
                'button:has-text("password")',
                ':has-text("Log in with a password")',
            ],
            "email_input": [
                'input[name="email"]',
                'input[placeholder*="Email" i]',
                'input[type="email"]',
                'input[type="text"]',
            ],
            "password_input": [
                'input[type="password"]',
                'input[name="password"]',
                'input[placeholder*="Password" i]',
                'input[placeholder*="password" i]',
            ],
            "signin_button": [
                'button.qwenchat-auth-pc-submit-button',
                'button:has-text("Log in")',
                'button:has-text("Sign in")',
                'button[type="submit"]',
            ],
            "error_message": [
                '[class*="error"]:visible',
                '[class*="alert"]:visible',
                'p[class*="error"]',
            ],
        },
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


# --------------------------------------------------------------------------- #
# Qwen Auth Config  (mirrors DeepSeek's AUTH_CONFIG)
# --------------------------------------------------------------------------- #
QWEN_AUTH_CONFIG: dict = {
    # Single credentials file holding every Qwen account's email + password.
    # Format sama dengan DeepSeek auth.json — AuthStore bisa membacanya langsung.
    # Contoh: [{"name": "account1", "email": "you@email.com", "password": "secret"}]
    # File ini dibagi dengan DeepSeek (kedua backend bisa pakai file yang sama
    # atau file terpisah; default pakai file yang sama untuk kemudahan).
    "auth_file": str(COOKIES_DIR / "authqwen.json"),

    # URL halaman login Qwen yang dituju saat Login/Sign Up terdeteksi.
    "login_url": "https://chat.qwen.ai/auth",

    # Env-var fallback jika tidak ada entry di auth.json.
    "env_email":    "QWEN_EMAIL",
    "env_password": "QWEN_PASSWORD",

    # Deteksi "sudah di halaman login" — URL path mengandung salah satu string ini.
    "login_url_patterns": ["/auth", "/login", "/signin", "/sign_in"],

    # Deteksi tombol Login/Sign Up di header utama (session expired / belum login).
    # Terkunci ke header kanan (#qwen-chat-header-right) agar tidak memicu false-positive
    # jika ada teks "Log in" di dalam pesan chat atau contoh kode.
    "unauthenticated_selectors": [
        "#qwen-chat-header-right .auth-buttons",
        "#qwen-chat-header-right .header-right-auth-button",
        ".auth-buttons",
        ".header-right-auth-button",
        '#qwen-chat-header-right button:has-text("Log in")',
        '#qwen-chat-header-right button:has-text("Sign up")',
    ],

    # Sinyal positif bahwa akun SUDAH LOGIN (hanya ada pada akun terautentikasi)
    "authenticated_selectors": [
        ".temporary-chat-entry",
        ".sidebar-wrapper",
        ".sidebar-entry-fixed-list-content",
        '[data-identity="sidebar"]',
    ],

    # Seconds to wait for post-login redirect to the chat UI.
    "login_wait": 60,

    # Brief settle after successful login before continuing.
    "post_login_settle": 0.5,

    # Jika captcha muncul saat login headless, gagal keras agar user re-run
    # dengan --no-headless untuk menyelesaikannya sekali (profile akan mengingatnya).
    "fail_loud_on_captcha": True,
}
