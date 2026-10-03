"""
config/grok.py — Grok-specific configuration for PAF-Model.

Holds GROK_CONFIG (selectors, timeouts, base_url, browser_channel,
cdp_attach) + GROK_AUTH_CONFIG (auth file, env fallback, login_wait,
fail_loud_on_captcha). This REPLACES the ChatGPT backend entirely — see
GROK_BACKEND.md for the full design rationale and migration notes.

v1 scope (per owner's reference scraper — grok.js / grokdebug.js /
login-capture-grok.js — and the design decisions ported from it):
  - Chat + code/canvas block extraction only.
  - NO automated email+password login. grok.com authenticates via SSO
    (Google / X (Twitter) / Apple / email-link), which cannot be reliably
    automated with a stable selector-based fill flow the way ChatGPT's
    "email → Continue → password" form could. The reference scraper never
    attempts this either — it only ever captures cookies from a manual
    login (login-capture-grok.js) or drives grok.com with those cookies
    already loaded. This backend follows the exact same model:
        1. login_grok.py       — one-time VISIBLE manual login (you solve
                                  SSO / Cloudflare yourself), OR
        2. import_grok_cookies.py — inject cookies exported from your
                                  everyday browser (Cookie-Editor).
      ensure_authenticated() NEVER tries to fill a credential form; it
      only checks whether the persisted profile/cookies are already valid,
      and fails loud with instructions otherwise.
  - Cloudflare / CDP mitigation ladder is REUSED VERBATIM from the ChatGPT
    backend (see scrapers/base_grok.py): playwright-stealth → real Chrome
    channel → patchright driver → manual login → cookie import → CDP
    attach to an already-running Chrome. Only the site URLs/selectors and
    the env var prefix changed (CHATGPT_* → GROK_*).
"""
from __future__ import annotations

from config.common import COOKIES_DIR, PROFILES_DIR  # noqa: F401  (re-export convenience)


# --------------------------------------------------------------------------- #
# Grok site config
# --------------------------------------------------------------------------- #
GROK_CONFIG: dict = {
    "base_url": "https://grok.com/",
    "new_chat_url": "https://grok.com/",

    "selectors": {
        # ── Chat UI ─────────────────────────────────────────────────────
        # Ported from grok.js's `inputSelectors` cascade (grok.com has no
        # stable data-testid on its composer, unlike ChatGPT's
        # #prompt-textarea — the reference scraper falls back through a
        # list of generic element types instead).
        "prompt_textarea": [
            "textarea",
            '[contenteditable="true"]',
            'input[type="text"]',
            '[role="textbox"]',
        ],
        # grok.js finds the send button by scanning every <button>/[role=
        # "button"] for text/aria-label containing "send" (no stable
        # selector exists) — mirrored here as a best-effort selector list;
        # GrokScraper._click_first() also has a text-scan fallback (see
        # scrapers/grok_scraper.py::_click_send_button).
        "send_button": [
            'button[aria-label*="Send" i]',
            'button:has-text("Send")',
        ],
        "stop_button": [
            'button[aria-label*="Stop" i]',
            'button[aria-label*="stop" i]',
        ],

        # ── Ekstraksi response ──────────────────────────────────────────
        # Grok has no `data-message-author-role` attribute (unlike
        # ChatGPT). grok.js instead grabs the LAST element inside the main
        # chat container matching one of these role/class patterns. Kept
        # as a selector list (not a single fixed selector) for resilience.
        "message_items": ['[role="article"]', '[role="region"]', 'div[class*="message"]'],
        "main_area": ['[role="main"]', "main", '[class*="chat"]'],

        # ── Assistant-response extraction (NEW — fix "output keluar tapi
        #    scrape kosong") ── grok.com's current DOM exposes data-testid
        #    markers for user/assistant turns. Tried in order by
        #    BaseAIChatScraper._extract_current_text() BEFORE the legacy
        #    message_items/main_area cascade. Keep sorted most→least
        #    specific; add newly observed variants at the TOP.
        "assistant_response": [
            '[data-testid="assistant-message"]',
            '[data-testid="message"][data-role="assistant"]',
            '[data-testid="message"]',
            '[data-message-id][data-role="assistant"]',
            '[data-message-id]',
        ],
        # Scope for the "still generating" text scan (replaces the old
        # whole-body scan that false-positived on responses containing
        # words like "thinking"/"menganalisis").
        "response_container": ['[data-testid="conversation-container"]', '[role="log"]'],
        # Last-resort class-fragment scan when no testid/role matches
        # (grok.com renames classes between deploys — this catches bubbles).
        "message_class_fallback": [
            "div[class*=\"bubble\"]",
            "div[class*=\"message\"]",
        ],

        # ── Login-state detection ───────────────────────────────────────
        # grok.js checks page TEXT for "Sign in"/"Log in"/"Create account"
        # (no stable selector). Here we use Playwright's text-matching
        # selectors so `_page_is_logged_in()` can reuse the same
        # querySelector-based pattern as the ChatGPT backend.
        "login_button": [
            'button:has-text("Sign in")',
            'a:has-text("Sign in")',
            'button:has-text("Log in")',
            'a:has-text("Log in")',
        ],

        # ── Attachments (best-effort — not covered by the reference
        #    grok.js scraper; kept generic/unverified for v1) ────────────
        "file_input": ['input[type="file"]'],
        "attachment_preview": ['[data-testid*="attachment"]', '[class*="attachment"]'],

        # ── Captcha / Cloudflare / Turnstile → fail loud. ───────────────
        # REUSED VERBATIM from config/chatgpt.py (CHATGPT_CONFIG) per the
        # explicit instruction to keep the proven CDP/Cloudflare mitigation
        # intact when swapping backends — Cloudflare Turnstile's DOM
        # footprint (challenge iframe / #challenge-form) is not
        # site-specific.
        "captcha": [
            'iframe[src*="challenges"]',
            'iframe[title*="challenge" i]',
            '[class*="turnstile"]',
            "#challenge-form",
        ],

        # ── Response-choice A/B dialog ("Which response do you prefer?")
        #    — a real Grok UI quirk documented in grok.js's
        #    handleResponseChoice(), not present on ChatGPT. ─────────────
        "response_choice_dialog_text": [
            "Which response", "Respons mana yang Anda pilih", "Pilihan",
        ],
        "response_choice_buttons": ["Response A", "Response B", "Respons A", "Respons B"],
        "response_choice_skip": ['button:has-text("Skip")', 'button:has-text("Lewati")'],

        # ── Rate limit (best-effort v1 — grok.com's exact copy for this
        #    is not confirmed against a live account; adjust as needed) ─
        "rate_limited": [
            "text=You've reached your limit",
            "text=Rate limit",
            "text=Try again later",
            "text=usage limit",
        ],

        # ── "Still generating" text signals (ported 1:1 from grok.js's
        #    isGenerating()/searchStatusPatterns — Grok shows web-search
        #    progress text ("Membaca", "Menelusuri", "Thinking...", etc.)
        #    inside the response area while NOT finished, which the
        #    stop-button-only heuristic used by ChatGPT would miss). ────
        "generating_text_patterns": [
            "generating", "typing", "thinking", "berfikir", "sedang berpikir",
            "membaca", "menelusuri", "mencari", "menganalisis",
            "loading", "researching", "searching", "browsing", "analyzing", "exploring",
        ],
    },

    "timeouts": {
        "page_load": 30_000,
        "response_wait": 240_000,     # grok.js maxWaitCycles=250 * 800ms ≈ 200s; +buffer
        "stability_check": 800,       # ms — matches grok.js checkInterval/requiredStableChecks
        "between_actions": 800,       # ms — matches grok.js's post-type settle wait
        "attachment_preview": 15_000,
    },

    # ── Browser channel / CDP attach (Cloudflare contingency) — REUSED
    #    from the ChatGPT backend's mitigation ladder, only the env var
    #    prefix changed (CHATGPT_* → GROK_*). See scrapers/base_grok.py. ─
    # None (default) → bundled Playwright Chromium.
    # "chrome"        → real installed Google Chrome via channel="chrome".
    "browser_channel": None,
    # True → attach to an ALREADY-RUNNING real Chrome over CDP instead of
    # launching one (start it with start_grok_chrome.py). Strongest
    # Cloudflare/anti-bot workaround. Toggle via GROK_CDP_ATTACH=1.
    "cdp_attach": False,
}


# --------------------------------------------------------------------------- #
# Grok Auth Config
# --------------------------------------------------------------------------- #
GROK_AUTH_CONFIG: dict = {
    # Same schema as authchatgpt.json / AuthStore (scrapers/utils.py) can
    # read it unmodified — "email"/"password" are RESERVED fields kept for
    # parity/future use, but v1 never fills them into a form (see module
    # docstring: grok.com is SSO-only, so this is a name registry for
    # accounts + an optional record of the credentials you used when
    # logging in by hand, not an automated-fill source).
    "auth_file": str(COOKIES_DIR / "authgrok.json"),

    "env_email":    "GROK_EMAIL",
    "env_password": "GROK_PASSWORD",

    # Best-effort URL patterns for "still mid-auth" detection (grok.com's
    # SSO redirects through Google/X/Apple's own domains, or grok.com's
    # own /login). Not load-bearing for v1 (no automated login flow drives
    # off of this), kept for future use / diagnostics only.
    "auth_url_patterns": ["accounts.x.ai", "grok.com/login", "/login", "/signin"],

    "login_wait": 900,   # 15 minutes — manual SSO login can take a while

    "post_login_settle": 1.0,

    # Captcha/Turnstile while on the login/SSO page → fail loud + debug
    # screenshot (same policy as ChatGPT — see scrapers/grok_scraper.py).
    "fail_loud_on_captcha": True,

    # DEVIATION from ChatGPT: there is no automated credential-fill login
    # to run headless in the first place — v1 has NO login() flow at all.
    # ensure_authenticated() only ever checks the existing session; the
    # ONLY ways to establish one are login_grok.py (visible, manual) or
    # import_grok_cookies.py (zero-automation cookie injection).
    "manual_login_required": True,
}
