"""
scrapers/base_chatgpt.py — abstract async base class for the ChatGPT scraper.

Mirrors scrapers/base_qwen.py's persistent-profile / email+password model
(NOT scrapers/base_deepseek.py's account-name+cookie model, though the two
are very close). Deliberate v1 deviations from the full base_qwen pattern
(per design_chatgpt_backend.md + implementation.md, Tahap B):

  • NO think_mode (ChatGPT v1 is chat-only, default model in the UI).
  • NO JSON-repair / JSON API mode (no tool-calling execution in v1).
  • NO legacy cookie-file auth mode — ChatGPT only ever uses the
    email+password / authchatgpt.json model, so all of that branching is
    dropped for clarity.
  • Login detection is done exclusively via the ABSENCE of the "Log in"
    button — the ChatGPT homepage still shows the "Ask ChatGPT" input even
    when logged out, so input-presence can NOT be used as a login signal
    (this is the critical, explicitly-called-out point from the design doc).

Provides:
  • Browser lifecycle (persistent-context launch / close / restart)
  • Profile-per-account isolation: profiles/chatgpt/<account>/ (kept apart
    from profiles/deepseek/<account>/ and profiles/qwen/<account>/ — this
    is the exact "profile collision" bug the DeepSeek/Qwen CHANGELOG
    entries warn about).
  • Credential resolution (arg > authchatgpt.json > env vars)
  • Account rotation (auth.json account list, restart-based)
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
    getPlaywright bindings, ...) — exactly the signals Cloudflare Turnstile
    uses to flag the browser no matter how stealthy the JS patches are or
    whether the window is visible.

    Selection via the CHATGPT_BROWSER_DRIVER env var:
      "auto"       (default) → patchright if installed, else playwright
      "patchright" / "pr"    → require patchright (clear error if missing)
      "playwright" / "pw"    → force vanilla playwright

    After installing patchright, ALSO install its browser binaries:
        pip install patchright
        python -m patchright install chromium
    """
    choice = (os.environ.get("CHATGPT_BROWSER_DRIVER") or "auto").strip().lower()

    def _try(pkg: str):
        try:
            return __import__(f"{pkg}.async_api", fromlist=["async_playwright"])
        except Exception:
            return None

    if choice in ("patchright", "pr"):
        mod = _try("patchright")
        if mod is None:
            raise ImportError(
                "CHATGPT_BROWSER_DRIVER=patchright but the 'patchright' package "
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
    CHATGPT_AUTH_CONFIG,
    CHATGPT_CONFIG,
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
    """Abstract async base class for the ChatGPT scraper (persistent profile).

    self._browser is always None (persistent-context mode only — no
    ephemeral fallback in v1, unlike base_qwen.py). self._context is the
    persistent BrowserContext returned by Playwright.
    """

    # ── Construction ──────────────────────────────────────────────── #

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

        self.email: str | None = email
        self.password: str | None = password
        self._authenticated: bool = False

        # Load every ChatGPT account from cookies/authchatgpt.json.
        self.auth = AuthStore(CHATGPT_AUTH_CONFIG["auth_file"])
        self._chatgpt_accounts: list[str] = self.auth.account_names()

        # Resolve account name: explicit arg > first in authchatgpt.json > "account1".
        self.account: str = account or (
            self._chatgpt_accounts[0] if self._chatgpt_accounts else "account1"
        )
        self._account_index: int = (
            self._chatgpt_accounts.index(self.account)
            if self.account in self._chatgpt_accounts else 0
        )

        self._playwright = None
        self._browser: Browser | None = None       # unused (persistent mode only)
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # Convenience alias used by pool code (mirrors deepseek's `.page`).
    @property
    def page(self) -> Page | None:
        return self._page

    # ── Profile path helpers ──────────────────────────────────────── #

    def _profile_dir_for(self, account: str | None = None) -> Path:
        """Return profiles/chatgpt/<account>/ — isolated from deepseek/qwen
        profiles to avoid the Chrome SingletonLock collision documented in
        the DeepSeek/Qwen CHANGELOG ("profile collision" fix).
        """
        base = PROFILES_DIR / "chatgpt"
        return base / (account or self.account)

    @staticmethod
    def _profile_seeded(profile_dir: Path) -> bool:
        return (profile_dir / "Default").exists() or (profile_dir / "cookies_seeded").exists()

    def _discover_accounts(self) -> None:
        """Refresh the account list from cookies/authchatgpt.json."""
        self._chatgpt_accounts = self.auth.account_names()
        if self._chatgpt_accounts:
            self.logger.info(
                "ChatGPT authchatgpt.json: %d account(s): %s",
                len(self._chatgpt_accounts), self._chatgpt_accounts,
            )
        else:
            self.logger.warning(
                "No ChatGPT accounts found. Add credentials to %s or set %s/%s.",
                CHATGPT_AUTH_CONFIG["auth_file"],
                CHATGPT_AUTH_CONFIG["env_email"],
                CHATGPT_AUTH_CONFIG["env_password"],
            )

    # ── Browser lifecycle ─────────────────────────────────────────── #

    async def launch_browser(self, account: str | None = None) -> None:
        """launch_persistent_context(user_data_dir=profiles/chatgpt/<account>/).

        Browser mode selection (checked in this order):
          1. CHATGPT_CDP_ATTACH=1 (or CHATGPT_CONFIG["cdp_attach"]) → attach
             to an ALREADY-RUNNING real Chrome via CDP (see
             `_launch_via_cdp_attach` / start_chatgpt_chrome.py). This is the
             strongest Cloudflare workaround: Playwright never launches the
             browser, so none of its automation fingerprints are present at
             startup.
          2. Otherwise → launch_persistent_context with the configured
             driver (patchright if installed / CHATGPT_BROWSER_DRIVER) and
             optional channel (CHATGPT_BROWSER_CHANNEL).
        """
        import os

        account = account or self.account
        cdp_attach = (
            os.environ.get("CHATGPT_CDP_ATTACH", "").strip() in ("1", "true", "yes")
            or CHATGPT_CONFIG.get("cdp_attach") is True
        )
        if cdp_attach:
            await self._launch_via_cdp_attach(account)
            return

        channel = os.environ.get("CHATGPT_BROWSER_CHANNEL") or CHATGPT_CONFIG.get("browser_channel")
        self.logger.info(
            "Launching ChatGPT browser (driver=%s, headless=%s, account=%s, channel=%s)",
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
                "New ChatGPT profile '%s' — authentication will happen via ensure_authenticated()",
                profile_dir.name,
            )
        else:
            self.logger.info("Reusing existing ChatGPT profile '%s'", profile_dir.name)

        self.logger.debug("ChatGPT browser launched successfully")

    async def _apply_stealth(self, context: BrowserContext) -> None:
        """Mask Playwright's CDP automation fingerprint.

        Simple property overrides (navigator.webdriver = undefined, etc.)
        are themselves detectable — Cloudflare Turnstile and similar
        services also check for CDP-specific leaks (Runtime.enable side
        effects, non-native getter toString(), iframe.contentWindow
        prototype mismatches, PluginArray shape, WebGL vendor/renderer in
        headless, etc.). Prefer the community-maintained `playwright-stealth`
        package (bundles ~15 targeted evasions) when installed; fall back to
        the smaller hand-written script otherwise so this backend still
        works without the optional dependency.
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

        The browser must have been started with a --remote-debugging-port
        (use start_chatgpt_chrome.py, which launches it with the SAME
        persistent profile profiles/chatgpt/<account>/ the worker uses).

        Why this is the strongest Cloudflare workaround: the browser is a
        REAL Chrome that started itself — no Playwright/patchright launch
        flags, no AutomationControlled hints, nothing injected before the
        page loads. Over the CDP attach connection Playwright only reads
        the DOM / clicks; the process-level automation fingerprints that
        Turnstile detects at launch time simply don't exist.

        Note: with this mode the profile is owned by the real Chrome
        process, so `headless` does not apply (the window is whatever the
        real Chrome was started with — start_chatgpt_chrome.py starts it
        visible by default).
        """
        cdp_url = os.environ.get("CHATGPT_CDP_URL", "").strip() or "http://127.0.0.1:9222"
        self.logger.info(
            "Attaching to running Chrome via CDP (%s) for account '%s' — "
            "start it with: python start_chatgpt_chrome.py --account %s",
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
                f"Start it first with:  python start_chatgpt_chrome.py --account {account}\n"
                f"(or set CHATGPT_CDP_URL to the right host:port)"
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
        start_chatgpt_chrome.py --stop or by closing the window).
        """
        self.logger.info("Closing ChatGPT browser (driver=%s)", DRIVER_NAME)
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
        self.logger.warning("🔄 Restarting ChatGPT browser (account: %s) …", target_account)
        retry_sleep(ROTATION_CONFIG.get("browser_restart_delay", 5))

        try:
            await self.close_browser()
            self._context = None
            self._browser = None
            self._page = None
            self._playwright = None
            await self.launch_browser(account=target_account)
            self.logger.info("✅ ChatGPT browser restart selesai")
            return True
        except Exception as exc:
            self.logger.error("❌ ChatGPT browser restart gagal: %s", exc, exc_info=True)
            return False

    # ── Credentials & account rotation ───────────────────────────── #

    def _resolve_credentials(
        self, email: str | None = None, password: str | None = None
    ) -> tuple[str | None, str | None]:
        """Priority: explicit args > authchatgpt.json entry > env vars."""
        import os

        email = email or self.email
        password = password or self.password

        if not email or not password:
            creds = self.auth.get(self.account)
            if creds:
                email = email or creds.get("email")
                password = password or creds.get("password")

        if not email:
            email = os.environ.get(CHATGPT_AUTH_CONFIG["env_email"])
        if not password:
            password = os.environ.get(CHATGPT_AUTH_CONFIG["env_password"])
        return email, password

    async def _rotate_account(self, restart_first: bool = True) -> bool:
        """Switch to the next account in authchatgpt.json and restart the
        browser into that account's profile. Returns False when there is
        no additional account to rotate to."""
        total = len(self._chatgpt_accounts)
        if total <= 1:
            self.logger.error("No additional ChatGPT accounts available for rotation")
            await self.take_debug_screenshot("no_accounts_to_rotate")
            return False

        next_index = (self._account_index + 1) % total
        if next_index == 0:
            self.logger.error("All ChatGPT accounts exhausted — no more rotation possible")
            await self.take_debug_screenshot("all_accounts_exhausted")
            return False

        next_account = self._chatgpt_accounts[next_index]
        self.logger.warning(
            "Rotating ChatGPT account: %s → %s", self.account, next_account,
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

    # ── Login-state detection (CRITICAL — see module docstring) ──── #

    async def _is_logged_in(self) -> bool:
        """TRUE only when we are actually on the rendered ChatGPT app AND
        the "Log in" button is NOT present in the DOM.

        BUG FIX: a pure "absence of the Log in button" check is a false
        positive on ANY page that isn't the ChatGPT app yet — a blank
        `about:blank`, a page still mid-navigation, a Cloudflare Turnstile
        interstitial on a different domain, or chatgpt.com itself before
        the SPA has hydrated far enough to render the button. All of those
        legitimately have no "Log in" button, but are not a logged-in
        session either. Observed live: right after `page.goto()`, this
        check fired True within ~1 poll interval even though the user
        hadn't finished the manual login flow at all.

        Fix: require BOTH (a) we're actually on chatgpt.com, AND (b) the
        "Log in" button is absent, AND (c) a positive signal that the app
        shell has actually rendered (prompt textarea or main chat area
        present) — not just "Log in" being absent. Do NOT use chat-input
        presence as the ONLY signal (the logged-out homepage still renders
        it — see module docstring); it is only checked here as a secondary
        confirmation that we're on the real, loaded app, after the
        "Log in" absence check has already passed.

        This delegates to `_page_is_logged_in()` so callers that need to
        check a page OTHER than `self._page` (e.g. a login popup window —
        see `login_chatgpt.py`) can reuse the exact same logic.
        """
        return await self._page_is_logged_in(self._page)

    async def _page_is_logged_in(self, page: "Page | None") -> bool:
        """Core login-state check, usable against ANY Page object — not
        just `self._page`. See `_is_logged_in()` docstring for the full
        rationale. Needed because clicking "Log in" can open a POPUP
        window (a separate Page object); polling only `self._page` while a
        popup is open checks the wrong tab entirely and can report a false
        positive from stale content left over in the original tab.
        """
        if page is None:
            return False
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if "chatgpt.com" not in url:
            # Not even on the ChatGPT app yet (mid-redirect, an auth
            # provider domain, a Cloudflare interstitial, about:blank, ...).
            return False
        for sel in CHATGPT_CONFIG["selectors"]["login_button"]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return False  # "Log in" button visible → not logged in
            except Exception:
                continue
        # Secondary confirmation the app shell actually rendered (guards
        # against the blank/loading-page race described above).
        shell_selectors = (
            CHATGPT_CONFIG["selectors"]["prompt_textarea"]
            + CHATGPT_CONFIG["selectors"]["main_area"]
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
        phrases = ["you've reached", "usage cap", "limit reached"]
        return any(p in body_lower for p in phrases)

    # ── Chat loop generic (poll for generation completion) ────────── #

    async def wait_for_response(self, *, deadline_s: float | None = None) -> str:
        """Poll loop (pola referensi openai.js + base_qwen):
          - generating = stop_button visible
          - selesai jika NOT generating DAN teks stabil 2x berturut-turut
          - deadline = CHATGPT_CONFIG['timeouts']['response_wait']
        """
        assert self._page is not None
        timeouts = CHATGPT_CONFIG["timeouts"]
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
                        "Session logged out mid-chat (Log in button reappeared)."
                    )
                if await self.is_rate_limited():
                    raise RuntimeError("Rate limited by ChatGPT (usage cap reached).")

            await asyncio.sleep(stability_interval)

        raise TimeoutError(
            f"ChatGPT response not stable after {deadline:.0f}s — "
            f"last text length: {len(prev_text) if prev_text else 0}"
        )

    async def _is_generating(self) -> bool:
        assert self._page is not None
        for sel in CHATGPT_CONFIG["selectors"]["stop_button"]:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _extract_current_text(self) -> str:
        """Grab the current text of the last assistant message (used while
        polling for stability, before final extraction/cleaning)."""
        assert self._page is not None
        for sel in CHATGPT_CONFIG["selectors"]["assistant_message"]:
            try:
                nodes = await self._page.query_selector_all(sel)
                if nodes:
                    return ((await nodes[-1].inner_text()) or "").strip()
            except Exception:
                continue
        return ""

    # ── Output helpers (pola existing) ─────────────────────────────── #

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

    # ── Abstract interface (diimplementasi ChatGPTScraper) ─────────── #

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
