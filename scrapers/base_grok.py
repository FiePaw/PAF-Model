"""
scrapers/base_grok.py — abstract async base class for the Grok scraper.

This REPLACES scrapers/base_chatgpt.py. The entire browser-lifecycle /
Cloudflare-mitigation machinery is REUSED VERBATIM from the ChatGPT backend
per the explicit migration requirement — only naming (CHATGPT_* → GROK_*,
chatgpt.com → grok.com, profiles/chatgpt → profiles/grok) changed. See
GROK_BACKEND.md §6 for the full mitigation-ladder writeup (this docstring
keeps the short version).

Deliberate v1 deviations from base_chatgpt.py (per the owner-supplied
grok.js / grokdebug.js / login-capture-grok.js reference scraper):

  • NO automated email+password login flow. grok.com authenticates via SSO
    (Google / X / Apple / email-link) — there is no stable "email input →
    Continue → password input" form to automate the way ChatGPT's was.
    The reference scraper never attempts this either (it only ever loads
    cookies captured from a manual login). login() is therefore NOT part
    of this class at all; the only ways to establish a session are
    login_grok.py (visible, manual) and import_grok_cookies.py (cookie
    injection, zero automation).
  • Login detection: same design principle as ChatGPT (never trust chat-
    input presence alone — grok.com's composer may render before/without
    a session too) — implemented via absence of a "Sign in"/"Log in"
    button AND presence of the rendered app shell (composer or main area).
  • "Still generating" detection is richer than the stop-button-only check
    used by ChatGPT: Grok surfaces web-search progress text ("Membaca",
    "Menelusuri", "Thinking…", etc.) inside the response area while not
    yet finished (ported from grok.js's isGenerating()/searchStatusPatterns).

Provides:
  • Browser lifecycle (persistent-context launch / close / restart)
  • Profile-per-account isolation: profiles/grok/<account>/
  • Cloudflare mitigation ladder: playwright-stealth → real Chrome channel
    → patchright driver → (manual login / cookie import, in the sibling
    scripts) → CDP attach to an already-running Chrome
  • Account rotation (authgrok.json account list, restart-based)
  • Generic wait_for_response() polling helper
  • Output helpers (JSON dump, code-block extraction, debug screenshots)
"""
from __future__ import annotations

import asyncio
import os
import re as _re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import tiktoken as _tiktoken
    _TK_ENC = _tiktoken.get_encoding("cl100k_base")
except Exception:
    _TK_ENC = None


def _count_tokens(text: str) -> int:
    """Hitung token via tiktoken cl100k_base. Fallback ke estimasi kasar."""
    if _TK_ENC is not None:
        try:
            return len(_TK_ENC.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _load_async_api():
    """Resolve which Playwright-compatible driver module to use.

    Patchright (https://github.com/Kaliiiiiiiiii-Virtual-Company/patchright)
    is a drop-in patched fork of Playwright that removes the CDP automation
    leaks vanilla Playwright leaves behind (Runtime.enable side effects,
    getPlaywright bindings, ...) — one of the signals Cloudflare Turnstile
    and similar anti-bot systems use to flag the browser no matter how
    stealthy the JS patches are or whether the window is visible.

    IDENTICAL mechanism to the ChatGPT backend — only the env var prefix
    changed (CHATGPT_BROWSER_DRIVER → GROK_BROWSER_DRIVER).

    Selection via the GROK_BROWSER_DRIVER env var:
      "auto"       (default) → patchright if installed, else playwright
      "patchright" / "pr"    → require patchright (clear error if missing)
      "playwright" / "pw"    → force vanilla playwright

    After installing patchright, ALSO install its browser binaries:
        pip install patchright
        python -m patchright install chromium
    """
    choice = (os.environ.get("GROK_BROWSER_DRIVER") or "auto").strip().lower()

    def _try(pkg: str):
        try:
            return __import__(f"{pkg}.async_api", fromlist=["async_playwright"])
        except Exception:
            return None

    if choice in ("patchright", "pr"):
        mod = _try("patchright")
        if mod is None:
            raise ImportError(
                "GROK_BROWSER_DRIVER=patchright but the 'patchright' package "
                "is not installed. Install it with:\n"
                "  pip install patchright\n"
                "  python -m patchright install chromium"
            )
        return mod, "patchright"
    if choice in ("playwright", "pw"):
        return _try("playwright"), "playwright"
    # auto: prefer patchright when available, fall back to vanilla playwright.
    mod = _try("patchright")
    if mod is not None:
        return mod, "patchright"
    return _try("playwright"), "playwright"


_DRIVER_MODULE, DRIVER_NAME = _load_async_api()

Browser = _DRIVER_MODULE.Browser
BrowserContext = _DRIVER_MODULE.BrowserContext
Page = _DRIVER_MODULE.Page
async_playwright = _DRIVER_MODULE.async_playwright

try:
    from playwright_stealth import Stealth as _PlaywrightStealth
    _STEALTH = _PlaywrightStealth(
        navigator_languages_override=("en-US", "en"),
    )
except Exception:
    _PlaywrightStealth = None
    _STEALTH = None

from config import (
    BROWSER_CONFIG,
    GROK_AUTH_CONFIG,
    GROK_CONFIG,
    CODE_OUTPUT_DIR,
    OUTPUT_DIR,
    PERSISTENT_CONTEXT_CONFIG,
    PROFILES_DIR,
    ROTATION_CONFIG,
)
from scrapers.utils import (
    AuthStore,
    detect_file_type,
    extract_code_blocks,
    retry_sleep,
    save_code_files,
    save_json,
    setup_logger,
    timestamped_filename,
)


class BaseAIChatScraper(ABC):
    """Abstract async base class for the Grok scraper (persistent profile).

    self._browser is always None (persistent-context mode only — no
    ephemeral fallback in v1, unlike base_qwen.py). self._context is the
    persistent BrowserContext returned by Playwright.
    """

    # ── Construction ────────────────────────────────────────────────── #

    def __init__(
        self,
        *,
        headless: bool = True,
        account: str | None = None,
        email: str | None = None,
        password: str | None = None,
    ) -> None:
        self.logger = setup_logger(self.__class__.__name__)
        self.headless = headless

        # Reserved for parity with the ChatGPT backend's AuthStore schema;
        # NOT used to automate a login form (see module docstring). Kept
        # only in case a future version adds a scriptable SSO path.
        self.email: str | None = email
        self.password: str | None = password
        self._authenticated: bool = False

        # Load every Grok account from cookies/authgrok.json.
        self.auth = AuthStore(GROK_AUTH_CONFIG["auth_file"])
        self._grok_accounts: list[str] = self.auth.account_names()

        # Resolve account name: explicit arg > first in authgrok.json > "account1".
        self.account: str = account or (
            self._grok_accounts[0] if self._grok_accounts else "account1"
        )
        self._account_index: int = (
            self._grok_accounts.index(self.account)
            if self.account in self._grok_accounts else 0
        )

        self._playwright = None
        self._browser: Browser | None = None       # unused (persistent mode only)
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # Convenience alias used by pool code (mirrors deepseek's `.page`).
    @property
    def page(self) -> Page | None:
        return self._page

    # ── Profile path helpers ────────────────────────────────────────── #

    def _profile_dir_for(self, account: str | None = None) -> Path:
        """Return profiles/grok/<account>/ — isolated from deepseek/qwen/
        (former) chatgpt profiles to avoid the Chrome SingletonLock
        collision documented in the DeepSeek/Qwen CHANGELOG ("profile
        collision" fix).
        """
        base = PROFILES_DIR / "grok"
        return base / (account or self.account)

    @staticmethod
    def _profile_seeded(profile_dir: Path) -> bool:
        return (profile_dir / "Default").exists() or (profile_dir / "cookies_seeded").exists()

    def _discover_accounts(self) -> None:
        """Refresh the account list from cookies/authgrok.json."""
        self._grok_accounts = self.auth.account_names()
        if self._grok_accounts:
            self.logger.info(
                "Grok authgrok.json: %d account(s): %s",
                len(self._grok_accounts), self._grok_accounts,
            )
        else:
            self.logger.warning(
                "No Grok accounts found. Add an entry to %s (name is enough — "
                "session comes from login_grok.py or import_grok_cookies.py, "
                "not from email/password).",
                GROK_AUTH_CONFIG["auth_file"],
            )

    # ── Browser lifecycle ───────────────────────────────────────────── #

    async def launch_browser(self, account: str | None = None) -> None:
        """launch_persistent_context(user_data_dir=profiles/grok/<account>/).

        Browser mode selection (checked in this order) — IDENTICAL to the
        ChatGPT backend's mitigation ladder:
          1. GROK_CDP_ATTACH=1 (or GROK_CONFIG["cdp_attach"]) → attach to
             an ALREADY-RUNNING real Chrome via CDP (see
             `_launch_via_cdp_attach` / start_grok_chrome.py). This is the
             strongest anti-bot workaround: Playwright never launches the
             browser, so none of its automation fingerprints are present
             at startup.
          2. Otherwise → launch_persistent_context with the configured
             driver (patchright if installed / GROK_BROWSER_DRIVER) and
             optional channel (GROK_BROWSER_CHANNEL).
        """
        import os

        account = account or self.account
        cdp_attach = (
            os.environ.get("GROK_CDP_ATTACH", "").strip() in ("1", "true", "yes")
            or GROK_CONFIG.get("cdp_attach") is True
        )
        if cdp_attach:
            await self._launch_via_cdp_attach(account)
            return

        channel = os.environ.get("GROK_BROWSER_CHANNEL") or GROK_CONFIG.get("browser_channel")
        self.logger.info(
            "Launching Grok browser (driver=%s, headless=%s, account=%s, channel=%s)",
            DRIVER_NAME, self.headless, account, channel or "chromium (bundled)",
        )
        self._playwright = await async_playwright().start()

        profile_dir = self._profile_dir_for(account)
        profile_dir.mkdir(parents=True, exist_ok=True)
        first_run = not self._profile_seeded(profile_dir)

        launch_kwargs: dict = dict(
            user_data_dir=str(profile_dir),
            headless=self.headless,
            slow_mo=BROWSER_CONFIG["slow_mo"],
            viewport=BROWSER_CONFIG["viewport"],
            user_agent=BROWSER_CONFIG["user_agent"],
            locale=BROWSER_CONFIG["locale"],
            timezone_id=BROWSER_CONFIG["timezone_id"],
            args=PERSISTENT_CONTEXT_CONFIG["launch_args"],
        )
        if channel:
            launch_kwargs["channel"] = channel

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            if channel:
                self.logger.error(
                    "Failed to launch with channel=%r (%s). Is Google Chrome installed on "
                    "this machine / did you run `playwright install chrome`?", channel, exc,
                )
            raise
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._browser = None
        if DRIVER_NAME == "playwright":
            await self._apply_stealth(self._context)
        else:
            # Patchright masks automation leaks at the DRIVER level; stacking
            # JS stealth patches on top can ADD detectable artifacts
            # (non-native property getters, etc.), so skip them entirely.
            self.logger.debug(
                "Skipping JS stealth injection (driver=%s masks automation at "
                "the driver level; extra JS patches can add detectable artifacts)",
                DRIVER_NAME,
            )

        if first_run:
            self.logger.info(
                "New Grok profile '%s' — no session yet. Run login_grok.py or "
                "import_grok_cookies.py before starting the worker.",
                profile_dir.name,
            )
        else:
            self.logger.info("Reusing existing Grok profile '%s'", profile_dir.name)

        self.logger.debug("Grok browser launched successfully")

    async def _apply_stealth(self, context: BrowserContext) -> None:
        """Mask Playwright's CDP automation fingerprint.

        IDENTICAL to the ChatGPT backend. Simple property overrides
        (navigator.webdriver = undefined, etc.) are themselves detectable
        — Cloudflare Turnstile and similar services also check for
        CDP-specific leaks (Runtime.enable side effects, non-native getter
        toString(), iframe.contentWindow prototype mismatches, PluginArray
        shape, WebGL vendor/renderer in headless, etc.). Prefer the
        community-maintained `playwright-stealth` package (bundles ~15
        targeted evasions) when installed; fall back to the smaller
        hand-written script otherwise so this backend still works without
        the optional dependency.
        """
        if _STEALTH is not None:
            try:
                await _STEALTH.apply_stealth_async(context)
                return
            except Exception as exc:
                self.logger.debug(
                    "playwright-stealth failed (%s) — falling back to built-in script", exc,
                )
        try:
            await context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = window.chrome || { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
                if (_origQuery) {
                    window.navigator.permissions.query = (parameters) => (
                        parameters && parameters.name === 'notifications'
                            ? Promise.resolve({ state: Notification.permission })
                            : _origQuery(parameters)
                    );
                }
                """
            )
        except Exception as exc:
            self.logger.debug("Stealth script injection skipped: %s", exc)

    async def _launch_via_cdp_attach(self, account: str) -> None:
        """Attach to an ALREADY-RUNNING real Chrome over CDP.

        IDENTICAL mechanism to the ChatGPT backend (see start_grok_chrome.py,
        which launches Chrome with the SAME persistent profile
        profiles/grok/<account>/ the worker uses).

        Why this is the strongest anti-bot workaround: the browser is a
        REAL Chrome that started itself — no Playwright/patchright launch
        flags, no AutomationControlled hints, nothing injected before the
        page loads. Over the CDP attach connection Playwright only reads
        the DOM / clicks; the process-level automation fingerprints that
        Cloudflare/Turnstile-style checks detect at launch time simply
        don't exist.

        Note: with this mode the profile is owned by the real Chrome
        process, so `headless` does not apply (the window is whatever the
        real Chrome was started with — start_grok_chrome.py starts it
        visible by default).
        """
        cdp_url = os.environ.get("GROK_CDP_URL", "").strip() or "http://127.0.0.1:9222"
        self.logger.info(
            "Attaching to running Chrome via CDP (%s) for account '%s' — "
            "start it with: python start_grok_chrome.py --account %s",
            cdp_url, account, account,
        )
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url, timeout=10_000)
        except Exception as exc:
            await self._playwright.stop()
            self._playwright = None
            raise RuntimeError(
                f"Could not attach to Chrome at {cdp_url}: {exc}\n"
                f"Start it first with:  python start_grok_chrome.py --account {account}\n"
                f"(or set GROK_CDP_URL to the right host:port)"
            ) from exc

        self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        # No stealth injection in attach mode: the browser is a real,
        # self-started Chrome — JS patches would only ADD detectable
        # artifacts on top of an already-clean fingerprint.

    async def close_browser(self) -> None:
        """Disconnect cleanly.

        In CDP-attach mode this only DISCONNECTS from the real Chrome —
        the browser itself keeps running (it wasn't started by us, so we
        don't own its lifecycle; the user closes it via
        start_grok_chrome.py --stop or by closing the window).
        """
        self.logger.info("Closing Grok browser (driver=%s)", DRIVER_NAME)
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

    async def _is_page_crashed(self) -> bool:
        """Detect a fatal page crash (Chromium error pages / OOM)."""
        try:
            if not self._page:
                return True
            try:
                body_text = await self._page.inner_text("body", timeout=5_000)
                body_lower = body_text.lower()
                for phrase in ROTATION_CONFIG.get("page_crash_phrases", []):
                    if phrase.lower() in body_lower:
                        self.logger.warning("Page crash detected: found phrase '%s'", phrase)
                        return True
            except Exception:
                self.logger.warning("Could not read page body — possible crash")
                return True
            return False
        except Exception as exc:
            self.logger.warning("Error during crash detection: %s", exc)
            return True

    async def restart_browser(self, account: str | None = None) -> bool:
        """Full close + relaunch. Used when the page crashes fatally."""
        target_account = account or self.account
        self.logger.warning("🔄 Restarting Grok browser (account: %s) …", target_account)
        retry_sleep(ROTATION_CONFIG.get("browser_restart_delay", 5))

        try:
            await self.close_browser()
            self._context = None
            self._browser = None
            self._page = None
            self._playwright = None
            await self.launch_browser(account=target_account)
            self.logger.info("✅ Grok browser restart selesai")
            return True
        except Exception as exc:
            self.logger.error("❌ Grok browser restart gagal: %s", exc, exc_info=True)
            return False

    # ── Credentials & account rotation ─────────────────────────────── #

    def _resolve_credentials(
        self, email: str | None = None, password: str | None = None
    ) -> tuple[str | None, str | None]:
        """Priority: explicit args > authgrok.json entry > env vars.

        NOTE: unlike the ChatGPT backend, this is NEVER used to drive an
        automated login form (grok.com is SSO-only — see module
        docstring). Kept only for parity/diagnostics and possible future
        use.
        """
        import os

        email = email or self.email
        password = password or self.password

        if not email or not password:
            creds = self.auth.get(self.account)
            if creds:
                email = email or creds.get("email")
                password = password or creds.get("password")

        if not email:
            email = os.environ.get(GROK_AUTH_CONFIG["env_email"])
        if not password:
            password = os.environ.get(GROK_AUTH_CONFIG["env_password"])
        return email, password

    async def _rotate_account(self, restart_first: bool = True) -> bool:
        """Switch to the next account in authgrok.json and restart the
        browser into that account's profile. Returns False when there is
        no additional account to rotate to."""
        total = len(self._grok_accounts)
        if total <= 1:
            self.logger.error("No additional Grok accounts available for rotation")
            await self.take_debug_screenshot("no_accounts_to_rotate")
            return False

        next_index = (self._account_index + 1) % total
        if next_index == 0:
            self.logger.error("All Grok accounts exhausted — no more rotation possible")
            await self.take_debug_screenshot("all_accounts_exhausted")
            return False

        next_account = self._grok_accounts[next_index]
        self.logger.warning(
            "Rotating Grok account: %s → %s", self.account, next_account,
        )
        self._account_index = next_index
        self.account = next_account
        retry_sleep(ROTATION_CONFIG["rotation_delay"])

        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        await self.launch_browser(account=next_account)
        self._authenticated = False
        return True

    # ── Login-state detection ──────────────────────────────────────── #

    async def _is_logged_in(self) -> bool:
        """TRUE only when we are actually on the rendered Grok app AND a
        "Sign in"/"Log in" button is NOT present in the DOM.

        Same design principle as the ChatGPT backend's `_is_logged_in()`
        (see base_chatgpt.py history): a pure "absence of the sign-in
        button" check is a false positive on ANY page that isn't the Grok
        app yet (about:blank, a page mid-navigation, an SSO/Cloudflare
        interstitial on a different domain, or grok.com itself before the
        SPA has hydrated far enough to render the button). Fix: require
        BOTH (a) we're actually on grok.com, AND (b) the sign-in button is
        absent, AND (c) a positive signal the app shell has rendered
        (composer or main chat area present).

        Delegates to `_page_is_logged_in()` so callers that need to check
        a page OTHER than `self._page` (e.g. a login popup window — see
        `login_grok.py`) can reuse the exact same logic.
        """
        return await self._page_is_logged_in(self._page)

    async def _page_is_logged_in(self, page: "Page | None") -> bool:
        """Core login-state check, usable against ANY Page object — not
        just `self._page`. See `_is_logged_in()` docstring for the full
        rationale. Needed because SSO can open a POPUP window (a separate
        Playwright Page object); polling only `self._page` while a popup
        is open checks the wrong tab entirely.
        """
        if page is None:
            return False
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if "grok.com" not in url:
            # Not even on the Grok app yet (mid-redirect, an SSO provider
            # domain, a Cloudflare interstitial, about:blank, ...).
            return False
        for sel in GROK_CONFIG["selectors"]["login_button"]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return False  # "Sign in"/"Log in" visible → not logged in
            except Exception:
                continue
        # Secondary confirmation the app shell actually rendered (guards
        # against the blank/loading-page race described above).
        shell_selectors = (
            GROK_CONFIG["selectors"]["prompt_textarea"]
            + GROK_CONFIG["selectors"]["main_area"]
        )
        for sel in shell_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    return True
            except Exception:
                continue
        return False

    async def is_session_expired(self) -> bool:
        return not await self._is_logged_in()

    async def is_rate_limited(self) -> bool:
        if self._page is None:
            return False
        try:
            body_text = await self._page.inner_text("body", timeout=5_000)
        except Exception:
            return False
        body_lower = body_text.lower()
        phrases = [p.replace("text=", "").lower() for p in GROK_CONFIG["selectors"]["rate_limited"]]
        return any(p in body_lower for p in phrases)

    # ── Chat loop generic (poll for generation completion) ──────────── #

    async def wait_for_response(self, *, deadline_s: float | None = None) -> str:
        """Poll loop (ported from grok.js's monitor-response-generation
        loop + base_chatgpt.py's stability pattern):
          - generating = stop_button visible OR page text still shows one
            of GROK_CONFIG's `generating_text_patterns` (Grok surfaces web-
            search progress text inside the response area — a stop button
            alone is not a reliable enough signal, unlike ChatGPT).
          - selesai jika NOT generating DAN teks stabil 2x berturut-turut
          - deadline = GROK_CONFIG['timeouts']['response_wait']
        """
        assert self._page is not None
        timeouts = GROK_CONFIG["timeouts"]
        deadline = deadline_s if deadline_s is not None else timeouts["response_wait"] / 1000
        stability_interval = timeouts["stability_check"] / 1000

        loop = asyncio.get_event_loop()
        deadline_at = loop.time() + deadline
        prev_text = None
        stable_count = 0
        poll_n = 0

        while loop.time() < deadline_at:
            poll_n += 1
            generating = await self._is_generating()

            try:
                text = await self._extract_current_text()
            except Exception:
                text = ""

            if not generating and text and text == prev_text:
                stable_count += 1
                if stable_count >= 2:
                    return text
            else:
                stable_count = 0

            prev_text = text

            # Periodic mid-chat safety checks (every ~5 polls).
            if poll_n % 5 == 0:
                if not await self._is_logged_in():
                    raise RuntimeError(
                        "Session logged out mid-chat (Sign in button reappeared)."
                    )
                if await self.is_rate_limited():
                    raise RuntimeError("Rate limited by Grok (usage limit reached).")

            await asyncio.sleep(stability_interval)

        raise TimeoutError(
            f"Grok response not stable after {deadline:.0f}s — "
            f"last text length: {len(prev_text) if prev_text else 0}"
        )

    async def _is_generating(self) -> bool:
        assert self._page is not None
        for sel in GROK_CONFIG["selectors"]["stop_button"]:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        # Ported from grok.js: web-search / thinking progress text inside
        # the page counts as "still generating" even with no stop button.
        try:
            body_text = (await self._page.inner_text("body", timeout=2_000)).lower()
        except Exception:
            return False
        return any(p in body_text for p in GROK_CONFIG["selectors"]["generating_text_patterns"])

    async def _extract_current_text(self) -> str:
        """Grab the current text of the last assistant message (used while
        polling for stability, before final extraction/cleaning)."""
        assert self._page is not None
        main = None
        for sel in GROK_CONFIG["selectors"]["main_area"]:
            try:
                main = await self._page.query_selector(sel)
                if main:
                    break
            except Exception:
                continue
        if not main:
            return ""
        for sel in GROK_CONFIG["selectors"]["message_items"]:
            try:
                nodes = await main.query_selector_all(sel)
                if len(nodes) >= 2:
                    return ((await nodes[-1].inner_text()) or "").strip()
            except Exception:
                continue
        return ""

    # ── Output helpers (pola existing) ──────────────────────────────── #

    def detect_file_type(self, content: str) -> str:
        return detect_file_type(content)

    def extract_code_blocks(self, content: str) -> list[dict]:
        return extract_code_blocks(content)

    def save_to_json(self, data: Any, filename: str | None = None) -> Path:
        if filename is None:
            filename = timestamped_filename("response")
        path = OUTPUT_DIR / filename
        save_json(data, path)
        self.logger.info("Saved JSON → %s", path)
        return path

    def save_code_files(
        self, blocks: list[dict], output_dir: Path | None = None, prefix: str = "snippet",
    ) -> list[Path]:
        target = output_dir or CODE_OUTPUT_DIR
        paths = save_code_files(blocks, target, prefix)
        self.logger.info("Saved %d code file(s) to %s", len(paths), target)
        return paths

    async def take_debug_screenshot(self, reason: str = "error") -> Path | None:
        """Ambil screenshot halaman saat ini dan simpan ke folder /debug/."""
        try:
            if not self._page:
                self.logger.warning("Screenshot gagal: _page belum tersedia")
                return None
            from config import DEBUG_DIR
            debug_dir = Path(DEBUG_DIR)
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe_reason = _re.sub(r"[^\w\-]", "_", reason)[:60]
            ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
            filename = f"debug_{ts}_{safe_reason}.png"
            path = debug_dir / filename
            await self._page.screenshot(path=str(path), full_page=True)
            self.logger.info("📸 Debug screenshot disimpan: %s", path)
            return path
        except Exception as exc:
            self.logger.warning("Gagal mengambil debug screenshot: %s", exc)
            return None

    # ── Abstract interface (diimplementasi GrokScraper) ─────────────── #

    @abstractmethod
    async def send_prompt(self, prompt: str, mode: str = "new", **kwargs) -> str: ...

    async def scrape(
        self, prompt: str, mode: str = "new", attachments: list | None = None, **kwargs,
    ) -> dict: ...

    @abstractmethod
    async def ensure_authenticated(self) -> bool: ...

    # ── Context manager ─────────────────────────────────────────────── #

    async def __aenter__(self) -> "BaseAIChatScraper":
        self._discover_accounts()
        await self.launch_browser(account=self.account)
        return self

    async def __aexit__(self, *_) -> None:
        await self.close_browser()
