"""
config/chatgpt.py — ChatGPT-specific configuration for PAF-Model.

Holds CHATGPT_CONFIG (selectors, timeouts, base_url) and CHATGPT_AUTH_CONFIG
(email+password / persistent-profile auth, mirroring config/qwen.py's
QWEN_AUTH_CONFIG). This is the THIRD backend added to PAF-Model — see
design_chatgpt_backend.md / implementation.md for the full design rationale.

v1 scope (per design doc, FINAL):
  - Chat + code blocks + attachments only.
  - NO think_mode / model picker (chat UI default model only).
  - NO SSO — email+password login only, via cookies/authchatgpt.json.
  - Login flow is REQUIRED to run headless=true (owner constraint, verified
    manually against the live "Continue with password" flow).
"""
from __future__ import annotations

from config.common import COOKIES_DIR, PROFILES_DIR  # noqa: F401  (re-export convenience)


# --------------------------------------------------------------------------- #
# ChatGPT site config
# --------------------------------------------------------------------------- #
CHATGPT_CONFIG: dict = {
    "base_url": "https://chatgpt.com/",
    "new_chat_url": "https://chatgpt.com/",

    "selectors": {
        # ── Chat UI ────────────────────────────────────────────────────
        "prompt_textarea": [
            "#prompt-textarea",
            'textarea[data-id="root"]',
            'div[contenteditable="true"]',
            "textarea",
        ],
        "send_button": [
            'button[data-testid="send-button"]',
            'button[aria-label*="Send"]',
        ],
        "stop_button": [
            'button[aria-label*="Stop"]',
            'button[aria-label="Stop generating"]',
        ],

        # ── Ekstraksi response ─────────────────────────────────────────
        "assistant_message": ['[data-message-author-role="assistant"]'],
        "user_message":      ['[data-message-author-role="user"]'],
        "main_area":         ["main", '[role="main"]'],

        # ── Attachments ────────────────────────────────────────────────
        "file_input":         ['input[type="file"]'],
        "attachment_preview": ['[data-testid*="attachment"]', '[class*="attachment"]'],

        # ── Login (lihat flow di scrapers/chatgpt_scraper.py) ──────────
        "login_button": [
            'button:has-text("Log in")',
            'a:has-text("Log in")',
            '[data-testid="login-button"]',
        ],
        "signup_button": [
            'button:has-text("Sign up")',
            'a:has-text("Sign up")',
        ],
        "login": {
            "email_input": [
                'input[name="email"]',
                'input[type="email"]',
                'input[placeholder*="email" i]',
            ],
            # BUG FIX: `button:has-text("Continue")` does SUBSTRING matching
            # in Playwright, so it also matches secondary/alternate login
            # buttons like "Continue with phone number", "Continue with
            # Google", "Continue with Apple", etc. If one of those happens
            # to sit earlier in the DOM than the real primary "Continue"
            # button, the wrong one gets clicked (observed live: email was
            # filled correctly but "Continue with phone number" was clicked
            # instead of "Continue"). Fixed with :text-is() for an EXACT
            # (whitespace-normalized) match, plus a defensive fallback that
            # explicitly excludes any "... with ..." variant.
            "continue_button": [
                'button:text-is("Continue")',
                'button[type="submit"]:text-is("Continue")',
                'button:has-text("Continue"):not(:has-text("with"))',
            ],
            # Halaman "Check your inbox" — jalur utama, BUKAN isi kode email.
            "continue_with_password": [
                'button:text-is("Continue with password")',
                'button:has-text("Continue with password")',
            ],
            "password_input": [
                'input[type="password"]',
                'input[name="password"]',
            ],
            "error_message": [
                '[role="alert"]',
                '[class*="error"]:visible',
                'p[class*="error"]',
            ],
        },

        # Captcha / Cloudflare / Turnstile → fail loud.
        "captcha": [
            'iframe[src*="challenges"]',
            'iframe[title*="challenge" i]',
            '[class*="turnstile"]',
            "#challenge-form",
        ],

        # Rate limit.
        "rate_limited": [
            "text=You've reached",
            "text=usage cap",
            "text=limit reached",
        ],
    },

    "timeouts": {
        "page_load": 30_000,
        "response_wait": 300_000,     # 5 minutes (selaras backend lain)
        "stability_check": 800,       # ms, selaras referensi openai.js
        "between_actions": 600,
        "attachment_preview": 15_000,
    },

    # ── Browser channel / CDP attach (Cloudflare Turnstile contingency) ───
    # None (default) → bundled Playwright Chromium, exactly as before.
    # "chrome"        → real installed Google Chrome binary via Playwright's
    #                    `channel="chrome"` (requires `playwright install
    #                    chrome` or a system Chrome install on the worker
    #                    machine).
    "browser_channel": None,
    # True → attach to an ALREADY-RUNNING real Chrome over CDP instead of
    # launching one (start it with start_chatgpt_chrome.py). Strongest
    # Cloudflare workaround — the browser self-started, so no Playwright
    # launch fingerprints exist at all. Usually toggled via the
    # CHATGPT_CDP_ATTACH=1 env var rather than edited here.
    "cdp_attach": False,
}


# --------------------------------------------------------------------------- #
# ChatGPT Auth Config (mirrors DeepSeek AUTH_CONFIG / Qwen QWEN_AUTH_CONFIG)
# --------------------------------------------------------------------------- #
CHATGPT_AUTH_CONFIG: dict = {
    # Format identik auth.json / authqwen.json — AuthStore (scrapers/utils.py)
    # bisa membacanya langsung tanpa modifikasi.
    "auth_file": str(COOKIES_DIR / "authchatgpt.json"),

    "env_email":    "CHATGPT_EMAIL",
    "env_password": "CHATGPT_PASSWORD",

    # Pola URL halaman auth (untuk deteksi "sedang di halaman login").
    "auth_url_patterns": ["auth.openai.com", "/login", "/signin", "/log-in"],

    "login_wait": 60,
    "post_login_settle": 1.0,

    # Captcha/Turnstile saat login → gagal keras + debug screenshot.
    "fail_loud_on_captcha": True,

    # CONSTRAINT OWNER: proses login selalu headless=True.
    "headless_during_login": True,
}
