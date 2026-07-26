"""
scrapers/base_scraper.py — BaseAIChatScraper (abstract).

Generic Playwright chat-scraper foundation. Platform-agnostic: all site-specific
behaviour is delegated to subclasses via abstract methods.

AUTH MODEL (profile-first, password-fallback)
---------------------------------------------
* Accounts are defined in ONE file: cookies/auth.json (loaded by AuthStore).
* Each account maps to a PERSISTENT browser profile at profiles/<account>/.
* The first run for an account logs in with email + password; the profile then
  stores the session. Later runs reuse the profile, and only log in again if the
  login DOM reappears (handled by the subclass's ensure_authenticated()).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from config import (
    AUTH_CONFIG,
    BROWSER_CONFIG,
    CODE_OUTPUT_DIR,
    COOKIES_DIR,
    DEBUG_DIR,
    OUTPUT_CONFIG,
    OUTPUT_DIR,
    PERSISTENT_CONTEXT_CONFIG,
    PROFILES_DIR,
    ROTATION_CONFIG,
)
from scrapers.utils import (
    AuthStore,
    cookie_editor_json_to_playwright,
    dump_json,
    get_logger,
)

# Best-effort .env loading so DEEPSEEK_EMAIL / DEEPSEEK_PASSWORD can live in a
# .env file at the project root.
try:  # pragma: no cover
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

log = get_logger("paf_deepseek.base")


# --------------------------------------------------------------------------- #
# Token counting (tiktoken cl100k_base, with len/4 fallback)
# --------------------------------------------------------------------------- #
# Parity with PAF-ModelQwen: accurate token counts via tiktoken when available,
# falling back to the len/4 heuristic (same as scrapers.utils.estimate_tokens)
# when tiktoken is not installed or encoding fails.
try:  # pragma: no cover
    import tiktoken as _tiktoken

    _TK_ENC = _tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover
    _TK_ENC = None


def _count_tokens(text: str) -> int:
    """Count tokens via tiktoken cl100k_base. Fallback to len/4 estimate."""
    if not text:
        return 0
    if _TK_ENC is not None:
        try:
            return len(_TK_ENC.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


class BaseAIChatScraper(ABC):
    """Abstract base for browser-automation chat scrapers."""

    # ------------------------------------------------------------------ #
    # Construction / lifecycle
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        headless: Optional[bool] = None,
        account: Optional[str] = None,
        profile_dir: Optional[str | Path] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.headless: bool = (
            BROWSER_CONFIG["headless"] if headless is None else headless
        )
        # Optional explicit credentials override (e.g. CLI --email/--password).
        self.email: Optional[str] = email
        self.password: Optional[str] = password
        self._authenticated: bool = False
        self._explicit_profile_dir: Optional[Path] = (
            Path(profile_dir) if profile_dir else None
        )

        # Playwright handles
        self._pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # Accounts come from the single auth.json file.
        self.auth = AuthStore(AUTH_CONFIG["auth_file"])
        self._accounts: list[str] = self._discover_accounts()

        # Current account name (key into auth.json + profile dir name).
        self.account: str = account or (self._accounts[0] if self._accounts
                                        else "account1")
        self._account_index: int = (
            self._accounts.index(self.account)
            if self.account in self._accounts else 0
        )

        self._browser_restarts: int = 0

    # ----- account discovery / profiles ------------------------------- #
    def _discover_accounts(self) -> list[str]:
        """All account names defined in cookies/auth.json."""
        return self.auth.account_names()

    def _profile_dir_for(self, account: Optional[str]) -> Path:
        """Per-account persistent-profile directory.

        Profiles are stored under profiles/deepseek/<account>/ to avoid
        collision with the Qwen backend which uses profiles/qwen/<account>/.
        Both backends may share the same account name (e.g. 'account1') and
        previously wrote to the same profiles/ path, causing Chrome
        SingletonLock conflicts when both workers ran simultaneously.
        """
        if self._explicit_profile_dir:
            return self._explicit_profile_dir
        return PROFILES_DIR / "deepseek" / (account or PERSISTENT_CONTEXT_CONFIG["default_profile"])

    @staticmethod
    def _profile_seeded(profile_dir: Path) -> bool:
        """Whether a persistent profile has stored state from a prior session."""
        if not profile_dir.exists():
            return False
        return any(profile_dir.iterdir())

    def _current_account(self) -> str:
        return self.account

    # ----- credentials ------------------------------------------------ #
    def _resolve_credentials(
        self, email: Optional[str] = None, password: Optional[str] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Resolve (email, password) for the current account in order:
          1. explicit args / self.email,self.password (CLI override)
          2. cookies/auth.json entry for self.account
          3. DEEPSEEK_EMAIL / DEEPSEEK_PASSWORD env vars (or .env)
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
            email = os.environ.get(AUTH_CONFIG["env_email"])
        if not password:
            password = os.environ.get(AUTH_CONFIG["env_password"])
        return email, password

    # ----- launch ----------------------------------------------------- #
    async def launch_browser(self, account: Optional[str] = None) -> Page:
        """
        Launch a persistent browser context bound to the account's profile.
        The profile carries the saved session across runs, so no cookie
        injection is required after the first login.
        """
        if account is not None:
            self.account = account
            if account in self._accounts:
                self._account_index = self._accounts.index(account)

        if self._pw is None:
            self._pw = await async_playwright().start()

        profile_dir = self._profile_dir_for(self.account)
        profile_dir.mkdir(parents=True, exist_ok=True)
        seeded = self._profile_seeded(profile_dir)
        log.info("Launching profile '%s' (seeded=%s, headless=%s)",
                 self.account, seeded, self.headless)

        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.headless,
            slow_mo=BROWSER_CONFIG["slow_mo"],
            viewport=BROWSER_CONFIG["viewport"],
            user_agent=BROWSER_CONFIG["user_agent"],
            locale=BROWSER_CONFIG["locale"],
            timezone_id=BROWSER_CONFIG["timezone_id"],
            args=PERSISTENT_CONTEXT_CONFIG["launch_args"],
        )
        self.browser = None  # persistent context owns the browser
        await self._apply_stealth(self.context)

        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        # A fresh profile starts unauthenticated; a seeded one *might* be valid.
        self._authenticated = False
        return self.page

    async def _apply_stealth(self, context: BrowserContext) -> None:
        """Light anti-automation patches + optional localStorage seeding."""
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        items = self._local_storage_items()
        if items:
            kvs = json.dumps(items)
            await context.add_init_script(
                f"(function(){{const kv={kvs};"
                "for (const k in kv) {try{localStorage.setItem(k, kv[k]);}"
                "catch(e){}}}})();"
            )

    def _local_storage_items(self) -> dict[str, str]:
        """Override in subclass to inject SPA localStorage tokens. Default empty."""
        return {}

    async def close_browser(self) -> None:
        for closer in (self.context, self.browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        self.context = None
        self.browser = None
        self.page = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None

    async def _is_page_crashed(self) -> bool:
        """Detect a crashed/closed page via Playwright state + error text."""
        if self.page is None:
            return True
        try:
            if self.page.is_closed():
                return True
            content = (await self.page.content()).lower()
            return any(
                phrase in content
                for phrase in ROTATION_CONFIG["page_crash_phrases"]
            )
        except Exception:
            return True

    async def restart_browser(self, account: Optional[str] = None) -> Page:
        """Close and relaunch the browser, honouring max_browser_restarts."""
        self._browser_restarts += 1
        if self._browser_restarts > ROTATION_CONFIG["max_browser_restarts"]:
            raise RuntimeError("Exceeded max_browser_restarts")
        log.warning("Restarting browser (attempt %d)", self._browser_restarts)
        await self.close_browser()
        await asyncio.sleep(ROTATION_CONFIG["browser_restart_delay"])
        return await self.launch_browser(account or self.account)

    # ------------------------------------------------------------------ #
    # Optional cookie helpers (legacy / debugging — NOT the main flow)
    # ------------------------------------------------------------------ #
    async def load_cookies(self, path: str | Path) -> None:
        """Inject a Cookie-Editor JSON export (optional manual seeding)."""
        path = Path(path)
        if not path.exists() or self.context is None:
            return
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        cookies = cookie_editor_json_to_playwright(raw)
        await self.context.add_cookies(cookies)
        log.info("Loaded %d cookies from %s", len(cookies), path.name)

    async def save_cookies(self, path: Optional[str | Path] = None) -> None:
        """Export current cookies (debug/backup) to cookies/<account>.cookies.json."""
        if self.context is None:
            return
        path = Path(path) if path else (COOKIES_DIR / f"{self.account}.cookies.json")
        cookies = await self.context.cookies()
        dump_json(cookies, path)
        log.info("Saved %d cookies to %s", len(cookies), path.name)

    # ------------------------------------------------------------------ #
    # Account rotation
    # ------------------------------------------------------------------ #
    async def _rotate_account(self, restart_first: bool = True) -> bool:
        """
        Switch to the next account from auth.json (and its profile). Returns
        True if rotation happened, False if there is no other account.
        """
        if len(self._accounts) <= 1:
            log.warning("No alternate account to rotate to")
            return False

        self._account_index = (self._account_index + 1) % len(self._accounts)
        next_account = self._accounts[self._account_index]
        log.warning("Rotating to account: %s", next_account)
        self.account = next_account
        self.email = None      # force re-resolve from auth.json for new account
        self.password = None
        self._authenticated = False
        await asyncio.sleep(ROTATION_CONFIG["rotation_delay"])

        self._browser_restarts = 0
        if restart_first:
            await self.restart_browser(next_account)
        else:
            await self.close_browser()
            await self.launch_browser(next_account)
        return True

    # ------------------------------------------------------------------ #
    # Response handling — text-change detection (virtual-scroll safe)
    # ------------------------------------------------------------------ #
    async def _count_response_elements(self) -> dict[str, int]:
        """
        Snapshot how many response elements each selector currently matches.

        Retained for diagnostic purposes and backward-compat with
        scrape_with_tool_result().  The primary wait loop (wait_for_response)
        now uses TEXT-CHANGE detection instead of count comparison, making it
        immune to virtual-scroll DOM recycling.

        Returns a dict ``{selector: count}``.
        """
        baselines: dict[str, int] = {}
        if self.page is None:
            return baselines
        for sel in self._response_selectors():
            try:
                count = await self.page.locator(sel).count()
                baselines[sel] = count
            except Exception:
                baselines[sel] = 0
        log.debug("Response baselines (diagnostic): %s", baselines)
        return baselines

    async def _get_last_response_text(self, _diagnostic: bool = False) -> str:
        """
        Read the text content of the LAST visible AI response element.

        Strategy (virtual-scroll aware, multiple fallbacks):
          1. Selectors scoped inside known virtual-list containers.
          2. Unscoped div.ds-markdown fallback (short conversations).
          3. Any element with ds-markdown class fragment.

        When _diagnostic=True, logs every selector's count for debugging.
        Returns stripped inner text of last matching element, or "" if nothing found.
        """
        if self.page is None:
            return ""

        for sel in self._virtual_list_selectors():
            try:
                loc = self.page.locator(sel)
                count = await loc.count()
                if _diagnostic:
                    log.info("[DIAG] selector=%r count=%d", sel, count)
                if count > 0:
                    text = await loc.nth(count - 1).inner_text()
                    if text and text.strip():
                        if _diagnostic:
                            log.info("[DIAG] matched sel=%r text_len=%d preview=%r",
                                     sel, len(text.strip()), text.strip()[:60])
                        return text.strip()
            except Exception as exc:
                if _diagnostic:
                    log.info("[DIAG] selector=%r exception=%s", sel, exc)
                continue
        return ""

    async def _dump_dom_diagnostic(self) -> None:
        """
        Dump diagnostic info about the current DOM state for debugging
        response detection failures.

        Selector lists are read dynamically from ``_virtual_list_selectors()``
        and ``_response_selectors()`` so the diagnostic always reflects the
        selectors that are ACTUALLY used at runtime, not a separate hardcoded
        list that can drift out of sync.
        """
        if self.page is None:
            return
        log.info("[DIAG] === DOM DIAGNOSTIC START === url=%s", self.page.url)

        # Build selector list from the live sources of truth:
        #   - virtual_list selectors (what wait_for_response actually uses)
        #   - response_selectors (what is visible at call-sites; shown for comparison)
        # De-duplicate while preserving order.
        _seen: set[str] = set()
        diag_selectors: list[str] = []
        for _sel in list(self._virtual_list_selectors()) + list(self._response_selectors()):
            if _sel not in _seen:
                _seen.add(_sel)
                diag_selectors.append(_sel)
        log.info("[DIAG] virtual_list_selectors: %s", self._virtual_list_selectors())
        log.info("[DIAG] response_selectors:      %s", self._response_selectors())

        for sel in diag_selectors:
            try:
                count = await self.page.locator(sel).count()
                log.info("[DIAG] selector=%r count=%d", sel, count)
            except Exception as exc:
                log.info("[DIAG] selector=%r error=%s", sel, exc)

        # Dump partial DOM structure around virtual list
        try:
            snippet = await self.page.evaluate("""() => {
                const vl = document.querySelector('.ds-virtual-list-visible-items');
                if (vl) return '[VL] ' + vl.innerHTML.substring(0, 500);
                const md = document.querySelector('div.ds-markdown');
                if (md) return '[MD] ' + md.innerHTML.substring(0, 500);
                return '[NONE] body classes: ' + document.body.className.substring(0,200);
            }""")
            log.info("[DIAG] DOM snippet: %s", snippet)
        except Exception as exc:
            log.info("[DIAG] DOM eval failed: %s", exc)
        await self.take_debug_screenshot("dom_diagnostic")
        log.info("[DIAG] === DOM DIAGNOSTIC END ===")

    async def _capture_pre_send_text(self) -> str:
        """
        Capture the text of the current last response BEFORE sending a new prompt.

        This snapshot is compared against poll results in wait_for_response()
        to detect when a NEW response has appeared.  Using text comparison
        instead of element counts makes detection immune to virtual-scroll DOM
        recycling (where ``count`` stays constant because one element is removed
        from the top while a new one is added at the bottom).

        Returns ``""`` on a fresh page (NEW mode) — any non-empty text in the
        poll loop will then be treated as the new response.
        """
        text = await self._get_last_response_text()
        log.info(
            "pre_send_text captured: len=%d preview=%r",
            len(text), text[:80],
        )
        return text

    async def wait_for_response(
        self,
        timeout: float,
        stability_secs: float,  # noqa: ARG002 – accepted for backward compat, may be used later
        stability_polls: int,
        poll_interval: float,
        initial_response_count: dict[str, int] | int = 0,  # noqa: ARG002 – backward compat
        pre_send_text: str = "",
        **_deprecated_kwargs,          # absorbs response_selectors= from old call-sites
    ) -> str:
        """
        Wait until the AI response stops changing.

        TEXT-CHANGE DETECTION (Bug #9 fix):
        ─────────────────────────────────────
        DeepSeek uses a virtual-scroll list (``ds-virtual-list``). Elements
        outside the viewport are removed from the DOM entirely.  After several
        exchanges the element count stays CONSTANT (one old element removed,
        one new element added), so ``count > baseline`` is never True →
        300 s timeout.

        The new approach compares TEXT CONTENT instead of element counts:

        1. ``pre_send_text`` — text of the last response BEFORE the prompt was
           sent (captured by ``_capture_pre_send_text()`` in scrape()).
        2. Poll ``_get_last_response_text()`` until the text changes from
           ``pre_send_text`` to something new AND stable.
        3. Stability: the text must remain identical for ``stability_polls``
           consecutive polls before it is accepted (catches still-streaming
           partial responses).

        This is completely immune to:
          • Virtual-scroll DOM recycling (count fluctuation)
          • ``:last-of-type`` baseline mismatch
          • DOM restructuring between exchanges

        ``initial_response_count`` and ``response_selectors`` are accepted
        for backward compatibility via **kwargs but are no longer used in the
        detection loop.  Pass ``response_selectors`` as a keyword argument at
        call-sites; it is silently ignored here so old call-sites keep working
        until they are cleaned up.
        """
        deadline = time.monotonic() + timeout
        last_text = ""
        stable_count = 0
        _found_new = False
        _poll_count = 0

        log.info(
            "wait_for_response START: pre_send_text len=%d preview=%r",
            len(pre_send_text), pre_send_text[:60],
        )

        while time.monotonic() < deadline:
            current_text = await self._get_last_response_text()
            _poll_count += 1

            # Log first few polls for visibility
            if _poll_count <= 5 or _poll_count % 20 == 0:
                log.info(
                    "wait_for_response poll#%d: found_new=%s current_len=%d "
                    "current_preview=%r",
                    _poll_count, _found_new, len(current_text), current_text[:60],
                )

            # Phase 1: wait for the response text to differ from pre-send text.
            if not _found_new:
                if current_text and current_text != pre_send_text:
                    _found_new = True
                    log.info(
                        "wait_for_response: NEW response detected at poll#%d "
                        "(pre_send len=%d, current len=%d)",
                        _poll_count, len(pre_send_text), len(current_text),
                    )
                    last_text = current_text
                    stable_count = 1
                # Still showing pre-send content or empty → keep waiting.
                await asyncio.sleep(poll_interval)
                continue

            # Phase 2: response found — wait for it to stabilise.
            if current_text == last_text:
                stable_count += 1
                if stable_count >= stability_polls:
                    log.info(
                        "wait_for_response: STABLE after %d polls (len=%d)",
                        stable_count, len(current_text),
                    )
                    return current_text
            else:
                stable_count = 1
                last_text = current_text

            await asyncio.sleep(poll_interval)

        log.warning(
            "wait_for_response timed out after %.0fs (found_new=%s, polls=%d, text_len=%d)",
            timeout, _found_new, _poll_count, len(last_text),
        )
        # Run DOM diagnostic to identify why detection failed.
        await self._dump_dom_diagnostic()
        # Final diagnostic read with full selector logging.
        await self._get_last_response_text(_diagnostic=True)
        return last_text

    async def _read_latest_response(
        self,
        selectors: list[str],  # noqa: ARG002 – backward compat, not used
        baselines: dict[str, int] | None = None,  # noqa: ARG002 – backward compat
    ) -> str:
        """
        Return the text of the latest assistant message.

        Delegates to ``_get_last_response_text()`` which uses virtual-list
        aware selectors.  The ``selectors`` and ``baselines`` parameters are
        accepted for backward compatibility but are no longer used in the
        detection logic.
        """
        return await self._get_last_response_text()

    # ------------------------------------------------------------------ #
    # Extraction / saving
    # ------------------------------------------------------------------ #
    _CODE_FENCE_RE = re.compile(
        r"```(?P<lang>[\w+\-.]*)\n(?P<code>.*?)```", re.DOTALL
    )

    _EXT_BY_LANG = {
        "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts", "bash": ".sh", "sh": ".sh",
        "shell": ".sh", "json": ".json", "html": ".html", "css": ".css",
        "java": ".java", "c": ".c", "cpp": ".cpp", "c++": ".cpp",
        "go": ".go", "rust": ".rs", "rs": ".rs", "sql": ".sql",
        "yaml": ".yaml", "yml": ".yaml", "markdown": ".md", "md": ".md",
    }

    def detect_file_type(self, content: str) -> str:
        """Heuristic file extension guess from code-block content."""
        c = content.strip()
        if c.startswith(("{", "[")):
            return ".json"
        if c.startswith("<!DOCTYPE html") or c.startswith("<html"):
            return ".html"
        if "def " in c or ("import " in c and "from " in c):
            return ".py"
        if "function " in c or "const " in c or "=>" in c:
            return ".js"
        if c.startswith("#!"):
            return ".sh"
        return ".txt"

    def extract_code_blocks(self, content: str) -> list[dict[str, str]]:
        """Parse markdown code fences into [{lang, code, ext}]."""
        blocks: list[dict[str, str]] = []
        for m in self._CODE_FENCE_RE.finditer(content or ""):
            lang = (m.group("lang") or "").strip().lower()
            code = m.group("code")
            ext = self._EXT_BY_LANG.get(lang) or self.detect_file_type(code)
            blocks.append({"lang": lang, "code": code, "ext": ext})
        return blocks

    def save_to_json(self, data: Any, filename: Optional[str] = None) -> Path:
        if filename is None:
            ts = datetime.now().astimezone().strftime(
                OUTPUT_CONFIG["timestamp_format"]
            )
            filename = f"response_{ts}.json"
        return dump_json(data, OUTPUT_DIR / filename)

    def save_code_files(
        self, blocks: list[dict[str, str]], prefix: Optional[str] = None
    ) -> list[Path]:
        ts = datetime.now().astimezone().strftime(OUTPUT_CONFIG["timestamp_format"])
        prefix = prefix or f"code_{ts}"
        saved: list[Path] = []
        for i, b in enumerate(blocks):
            path = CODE_OUTPUT_DIR / f"{prefix}_{i}{b['ext']}"
            path.write_text(b["code"], encoding=OUTPUT_CONFIG["encoding"])
            saved.append(path)
        if saved:
            log.info("Saved %d code file(s) to %s", len(saved), CODE_OUTPUT_DIR)
        return saved

    # ------------------------------------------------------------------ #
    # Error resilience
    # ------------------------------------------------------------------ #
    async def take_debug_screenshot(self, reason: str = "error") -> Optional[Path]:
        if self.page is None:
            return None
        ts = datetime.now().astimezone().strftime(OUTPUT_CONFIG["timestamp_format"])
        safe = re.sub(r"[^\w\-]+", "_", reason)[:40]
        path = DEBUG_DIR / f"{ts}_{safe}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True)
            log.info("Saved debug screenshot: %s", path.name)
            return path
        except Exception:
            return None

    @staticmethod
    def _repair_unbalanced_brackets(raw: str) -> "str | None":
        """
        Fix JSON where array closing brackets are missing.

        DeepSeek sometimes outputs '}}' instead of '}]}': the choices array
        '[' is opened but never closed with ']'. This causes:
          "Expecting ',' delimiter" at the second-to-last character.

        Strategy: count unbalanced '[' vs ']'. If unbalanced by N, insert N
        ']' immediately before the final N closing '}' characters.

        Common case (N=1, ends with '}}'):
          ...,"finish_reason":"stop"}}
          →  ...,"finish_reason":"stop"}]}   ✓

        Returns repaired string, or None if brackets are already balanced or
        the raw doesn't end with '}' (insertion point ambiguous).
        """
        d_brack = raw.count('[') - raw.count(']')
        if d_brack <= 0:
            return None  # already balanced or over-closed

        raw_s = raw.rstrip()
        if not raw_s.endswith('}'):
            return None

        # Insert d_brack ']' chars before the final d_brack '}' chars.
        tail = raw_s[-d_brack:]
        prefix = raw_s[:-d_brack]
        candidate = prefix + (']' * d_brack) + tail
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

        # Fallback: try inserting at various offsets from end
        for offset in range(1, min(30, len(raw_s))):
            attempt = raw_s[:-offset] + (']' * d_brack) + raw_s[-offset:]
            try:
                json.loads(attempt)
                return attempt
            except json.JSONDecodeError:
                pass

        return None

    @staticmethod
    def _repair_unescaped_quotes(raw: str) -> "str | None":
        """
        Fallback repair untuk kasus paling umum: model menulis quote literal (")
        di dalam isi `content` tanpa di-escape, sehingga merusak parsing JSON.

        Strategi: cari blok `"content":"..."` dengan regex non-greedy yang
        berhenti tepat sebelum penanda akhir field yang valid (`","finish_reason"`
        atau `"}` penutup objek message), lalu escape ulang SEMUA quote dan
        backslash di dalam isi tsb sebelum di-reinsert ke string asli.

        Returns string JSON yang sudah diperbaiki, atau None jika pola
        `"content":"..."` tidak ditemukan sama sekali (repair tidak applicable).
        """
        marker = '"content":"'
        start_idx = raw.find(marker)
        if start_idx == -1:
            return None
        content_start = start_idx + len(marker)

        end_markers = ['","finish_reason"', '"},"finish_reason"', '"}}']
        end_idx = -1
        for em in end_markers:
            idx = raw.rfind(em)
            if idx > content_start and (end_idx == -1 or idx > end_idx):
                end_idx = idx

        if end_idx == -1:
            return None

        inner = raw[content_start:end_idx]
        repaired_inner = (
            inner.replace("\\", "\\\\")
                 .replace('"', '\\"')
                 .replace("\n", "\\n")
                 .replace("\r", "\\r")
                 .replace("\t", "\\t")
        )

        repaired = (
            raw[:content_start] + repaired_inner + raw[end_idx:]
        )
        return repaired

    @staticmethod
    def _repair_content_field(raw: str) -> "str | None":
        """
        Robust repair untuk JSON di mana string value 'content' mengandung
        karakter yang merusak parsing: unescaped quotes, literal newlines, dll.

        Strategi:
        1. Temukan '"content":"' marker.
        2. Cari posisi tail envelope yang valid (rfind dari belakang) sebagai
           batas akhir isi content.
        3. Escape seluruh inner content (backslash, quote, newline, dll).
        4. Rebuild JSON; jika masih unbalanced brace/bracket, tambahkan
           penutup yang hilang secara otomatis.
        5. Coba beberapa tail_pattern sebelum menyerah.

        Truncation fallback (Stage A + B):
        - Stage A (near-truncation): scan mundur hingga 100 char dari akhir,
          cari '"' yang diikuti '}', ',' atau ']' — ini kemungkinan penutup
          string content yang asli; sisa setelahnya adalah partial JSON tail.
        - Stage B (hard truncation): JSON terpotong total oleh virtual scroll
          sebelum penutup apapun muncul. Escape seluruh sisa string sebagai
          content dan paksa tutup envelope.

        Returns string JSON yang sudah diperbaiki, atau None jika tidak
        applicable (tidak ada 'content' key, atau semua strategi gagal).
        """
        marker = '"content":"'
        start_idx = raw.find(marker)
        if start_idx == -1:
            return None
        content_start = start_idx + len(marker)
        raw_s = raw.rstrip()

        tail_patterns = [
            '","finish_reason":"stop"}]}',
            '","finish_reason":"stop"}}',
            '"},"finish_reason":"stop"}]}',
            '"},"finish_reason":"stop"}}',   # DeepSeek's common }} variant
            '"}]}',
            '"}}',
            '"}]',
            '"}}]',
        ]

        def _balance(s: str) -> str:
            """Insert missing closing braces/brackets at end of string."""
            d_brace = d_brack = 0
            for ch in s:
                if ch == '{': d_brace += 1
                elif ch == '}': d_brace -= 1
                elif ch == '[': d_brack += 1
                elif ch == ']': d_brack -= 1
            if d_brace == 0 and d_brack == 0:
                return s
            suffix = ('}' * d_brace if d_brace > 0 else '') + (']' * d_brack if d_brack > 0 else '')
            return s.rstrip() + suffix

        def _esc(s: str) -> str:
            return (
                s.replace("\\", "\\\\").replace('"', '\\"')
                 .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            )

        def _try_with_content(prefix: str, content_val: str) -> "str | None":
            """Escape content_val and try to build a parseable JSON envelope."""
            e = _esc(content_val)
            for ctail in [
                '","finish_reason":"stop"}]}',
                '"},"finish_reason":"stop"}]}',
                '"}}',
            ]:
                cand = prefix + e + ctail
                try:
                    json.loads(cand)
                    return cand
                except json.JSONDecodeError:
                    pass
                bal = _balance(cand)
                if bal != cand:
                    try:
                        json.loads(bal)
                        return bal
                    except json.JSONDecodeError:
                        pass
            return None

        # ── Pass 1: known tail patterns (rfind) ─────────────────────────── #
        for raw_tail in tail_patterns:
            idx = raw_s.rfind(raw_tail)
            if idx <= content_start:
                continue
            inner = raw_s[content_start:idx]
            cand = raw_s[:content_start] + _esc(inner) + raw_tail
            try:
                json.loads(cand)
                return cand
            except json.JSONDecodeError:
                pass
            bal = _balance(cand)
            if bal != cand:
                try:
                    json.loads(bal)
                    return bal
                except json.JSONDecodeError:
                    pass
            for insertion in ['}', '}}', '}}}']:
                for pos_marker in ['}]}', '}]', ']}', ']']:
                    last_pos = cand.rfind(pos_marker)
                    if last_pos > 0:
                        attempt = cand[:last_pos] + insertion + cand[last_pos:]
                        try:
                            json.loads(attempt)
                            return attempt
                        except json.JSONDecodeError:
                            pass

        # ── Pass 2: Truncation fallback ──────────────────────────────────── #
        prefix = raw_s[:content_start]
        inner_raw = raw_s[content_start:]

        # Stage A — near-truncation: look for '"' followed by '}', ',' or ']'
        # in the last 100 chars, scanning FORWARD (earliest occurrence wins).
        # Scanning forward ensures we find the content's ACTUAL closing '"'
        # before any JSON envelope markers deeper in the tail (e.g. "stop"}}).
        scan_start = max(0, len(inner_raw) - 100)
        for pos in range(scan_start, len(inner_raw) - 1):
            if inner_raw[pos] == '"' and inner_raw[pos + 1] in ('}', ',', ']'):
                result = _try_with_content(prefix, inner_raw[:pos])
                if result:
                    return result

        # Stage B — hard truncation: JSON cut off before any closing delimiter.
        # Treat the entire remaining string as content (truncated mid-sentence).
        trimmed = inner_raw
        if trimmed.endswith('"') and (len(trimmed) < 2 or trimmed[-2] != '\\'):
            trimmed = trimmed[:-1]
        result = _try_with_content(prefix, trimmed)
        if result:
            return result

        return None

    @staticmethod
    def _repair_by_regex(raw: str) -> "str | None":
        """
        Last-resort repair: extract the 'content' string value via regex and
        reconstruct a guaranteed-valid JSON envelope from a Python dict.

        Bypasses ALL JSON structural issues:
        - Virtual-scroll truncation (JSON cut off mid-sentence or mid-field)
        - Unescaped quotes, newlines, or backslashes inside content
        - Wrong brace placement (finish_reason inside vs outside message)
        - Any other malformed envelope around a parseable content string

        Only applies to success-style responses (must have a '"content"' key).
        Tool-calls responses (no 'content') correctly return None.

        Returns a compact, single-line, guaranteed-valid JSON string, or None
        if no '"content"' key is found in raw.
        """
        import re as _re
        # Capture everything after "content":" until first unescaped " or EOF.
        # DOTALL handles literal newlines inside the value.
        m = _re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)', raw, _re.DOTALL)
        if not m:
            return None

        raw_content = m.group(1)  # with JSON escape sequences intact

        # Decode JSON escape sequences → real Python string
        try:
            content = json.loads('"' + raw_content + '"')
        except (json.JSONDecodeError, ValueError):
            # Truncated mid-escape-sequence: strip trailing backslash and retry
            content = json.loads('"' + raw_content.rstrip('\\') + '"') if raw_content.rstrip('\\') != raw_content else raw_content.rstrip('\\')

        # Reconstruct a proper, guaranteed-valid JSON envelope
        return json.dumps(
            {
                "status": "success",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
            },
            ensure_ascii=False,
            separators=(',', ':'),
        )

    @staticmethod
    def _fix_invalid_backslashes(raw: str) -> "str | None":
        """
        Scan JSON string char-by-char; inside string literals, replace any
        backslash sequence that isn't a valid JSON escape (\\", \\\\, \\/, \\b,
        \\f, \\n, \\r, \\t, \\uXXXX) with a doubled backslash.

        Handles Windows paths like "C:\\Users\\..." that DeepSeek writes as
        "C:\\Users\\..." with unescaped backslashes, causing 'Invalid \\escape'
        JSON parse errors.

        Returns the repaired string, or None if nothing changed (no-op on
        already-valid JSON).
        """
        VALID_AFTER_BS = frozenset(['"', '\\', '/', 'b', 'f', 'n', 'r', 't', 'u'])
        out: list[str] = []
        in_str = False
        changed = False
        i = 0
        n = len(raw)
        while i < n:
            c = raw[i]
            if not in_str:
                out.append(c)
                if c == '"':
                    in_str = True
                i += 1
            else:
                if c == '\\':
                    nxt = raw[i + 1] if i + 1 < n else ''
                    if nxt in VALID_AFTER_BS:
                        # Valid escape (\\", \\\\, \\/, \\b, \\f, \\n, \\r, \\t, \\uXXXX)
                        # emit both chars unchanged; \uXXXX hex digits follow as normal chars
                        out.append(c)
                        out.append(nxt)
                        i += 2
                    else:
                        # Invalid escape — double the backslash; next char processed normally
                        out.append('\\\\')
                        changed = True
                        i += 1
                elif c == '"':
                    # Unescaped quote → closing delimiter of this string literal
                    out.append(c)
                    in_str = False
                    i += 1
                else:
                    out.append(c)
                    i += 1
        return ''.join(out) if changed else None

    @staticmethod
    def _repair_tool_calls_arguments(raw: str) -> "str | None":
        """
        Repair kasus tool_calls di mana arguments berisi inner quotes yang tidak
        di-escape, sehingga menyebabkan JSON truncated/malformed.

        Strategi: untuk setiap string value di dalam arguments, escape semua
        inner quotes dan karakter kontrol yang tidak ter-escape.
        """
        import re as _re
        args_marker = '"arguments":{'
        start = raw.find(args_marker)
        if start == -1:
            return None
        args_start = start + len(args_marker) - 1  # posisi `{`

        def escape_string_values(s: str) -> str:
            """Escape unescaped quotes dan newline di dalam string JSON values."""
            result = []
            i = 0
            while i < len(s):
                if s[i] == '"':
                    result.append('"')
                    i += 1
                    while i < len(s):
                        if s[i] == '\\' and i + 1 < len(s):
                            result.append(s[i])
                            result.append(s[i + 1])
                            i += 2
                        elif s[i] == '"':
                            rest = s[i + 1:].lstrip()
                            if rest and rest[0] in (':', ',', '}', ']'):
                                result.append('"')
                                i += 1
                                break
                            else:
                                result.append('\\\"')
                                i += 1
                        elif s[i] in ('\n', '\r', '\t'):
                            result.append('\\n' if s[i] == '\n' else ('\\r' if s[i] == '\r' else '\\t'))
                            i += 1
                        else:
                            result.append(s[i])
                            i += 1
                else:
                    result.append(s[i])
                    i += 1
            return ''.join(result)

        try:
            json.loads(raw)
            return None  # tidak perlu repair
        except Exception:
            pass

        tail = raw[args_start:]
        repaired_tail = escape_string_values(tail)
        repaired = raw[:args_start] + repaired_tail

        try:
            json.loads(repaired)
            return repaired
        except Exception:
            return None

    @abstractmethod
    def _validate_response(self, raw: str) -> tuple[bool, str]:
        """Validate response structure before parsing. Returns (ok, cleaned)."""
        ...

    # ------------------------------------------------------------------ #
    # Abstract platform hooks
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def send_prompt(self, prompt: str, mode: str = "new", **kwargs) -> str:
        """Send a prompt; return a handle used by wait_for_response."""
        ...

    @abstractmethod
    async def is_rate_limited(self) -> bool:
        ...

    @abstractmethod
    async def is_session_expired(self) -> bool:
        ...

    @abstractmethod
    def _extra_send_kwargs(self) -> dict:
        """Platform-specific default send kwargs (e.g. deep_think/web_search)."""
        ...

    async def ensure_authenticated(self) -> bool:
        """
        Ensure the current session is logged in. Default no-op (returns True).
        Subclasses that support email+password login override this to perform
        login when the session/login DOM is detected. Must be idempotent.
        """
        return True

    # ------------------------------------------------------------------ #
    # Orchestrator
    # ------------------------------------------------------------------ #
    async def scrape(
        self,
        prompt: str,
        mode: str = "new",
        attachments: Optional[list[str | Path]] = None,
        **send_kwargs,
    ) -> dict[str, Any]:
        """
        Main orchestrator:
          launch/reuse profile -> authenticate (login if needed) -> send_prompt
          -> wait_for_response -> validate/repair -> extract -> save.
        """
        from config import DEEPSEEK_CONFIG  # keep base import-light

        if self.page is None:
            await self.launch_browser(self.account)

        merged_kwargs = {**self._extra_send_kwargs(), **send_kwargs}
        if attachments:
            merged_kwargs["attachments"] = attachments

        retries = 0
        max_retries = ROTATION_CONFIG["max_retries_per_account"]
        last_error: Optional[str] = None

        while retries <= max_retries:
            try:
                # --- TIMING instrumentation -----------------------------------
                # Records how long each stage of a single process takes so the
                # slow part is measurable instead of guessed. Emitted once per
                # attempt at log level INFO as a single [TIMING] line.
                _t0 = time.monotonic()
                _t_auth = _t_send = _t_wait = _t0

                if await self._is_page_crashed():
                    await self.restart_browser(self.account)

                # Authenticate (logs in with email+password if the login DOM is
                # present / session expired). If it ultimately fails, rotate.
                if not await self.ensure_authenticated():
                    log.warning("Authentication failed; rotating account")
                    if not await self._rotate_account(restart_first=False):
                        raise RuntimeError("Login failed and no account to rotate")
                _t_auth = time.monotonic()

                # --- TEXT-CHANGE DETECTION (Bug #9 fix) ----------------------
                # DeepSeek uses virtual-scroll (ds-virtual-list). Elements
                # outside the viewport are removed from the DOM, so element
                # COUNTS stay constant even when a new response arrives (one
                # old element is removed as one new one is added). Count-based
                # detection therefore never fires → 300 s timeout.
                #
                # Fix: capture the text of the LAST visible response BEFORE
                # sending the prompt, then wait for that text to CHANGE. This
                # is completely immune to count fluctuations.
                #
                # For NEW mode the page is blank after _goto_new_chat(), so
                # pre_send_text == "" and any non-empty text in the poll loop
                # is immediately treated as the new response.
                if mode == "new":
                    pre_send_text = ""
                    initial_response_count = {}
                    log.debug("NEW mode: pre_send_text='', baseline={}")
                    await self.send_prompt(prompt, mode=mode, **merged_kwargs)
                else:
                    # CONTINUE: snapshot BOTH text and counts before sending.
                    # OPTIMISATION: run both DOM reads concurrently with
                    # asyncio.gather() instead of sequentially. Each call
                    # does an independent page.evaluate(); they do not share
                    # state and are safe to parallelise. Saves ~0.3–0.5s.
                    pre_send_text, initial_response_count = await asyncio.gather(
                        self._capture_pre_send_text(),
                        self._count_response_elements(),
                    )
                    log.debug(
                        "CONTINUE mode: pre_send_text len=%d, baseline=%s",
                        len(pre_send_text), initial_response_count,
                    )
                    await self.send_prompt(prompt, mode=mode, **merged_kwargs)
                _t_send = time.monotonic()

                t = DEEPSEEK_CONFIG["timeouts"]
                text = await self.wait_for_response(
                    response_selectors=self._response_selectors(),
                    timeout=t["response_wait"],
                    stability_secs=t["stability_check"],
                    stability_polls=t["stability_polls"],
                    poll_interval=t["poll_interval"],
                    initial_response_count=initial_response_count,
                    pre_send_text=pre_send_text,
                )
                _t_wait = time.monotonic()
                log.info(
                    "[TIMING] auth=%.2fs send=%.2fs wait=%.2fs total=%.2fs "
                    "(mode=%s)",
                    _t_auth - _t0,
                    _t_send - _t_auth,
                    _t_wait - _t_send,
                    _t_wait - _t0,
                    mode,
                )

                if await self.is_rate_limited():
                    log.warning("Rate limited; attempting recovery")
                    await self.take_debug_screenshot("rate_limited")
                    if self._browser_restarts < ROTATION_CONFIG["max_browser_restarts"]:
                        await self.restart_browser(self.account)
                    else:
                        await self._rotate_account(restart_first=False)
                    retries += 1
                    await asyncio.sleep(ROTATION_CONFIG["retry_delay"])
                    continue

                # ── Response handling ───────────────────────────────────────
                # JSON API mode (parity w/ PAF-ModelQwen): DeepSeek was asked
                # (via the [SYSTEM CONTEXT] wrapper built in send_prompt) to
                # reply with a JSON envelope. Validate it; if invalid, send a
                # corrective-feedback prompt in the SAME conversation up to
                # max_corrective_retries before letting the outer retry/rotate
                # loop take over. When disabled, fall back to lenient plain-text
                # validation (original DeepSeek behaviour).
                from config import JSON_API_CONFIG
                _json_mode = (
                    JSON_API_CONFIG.get("enabled", False)
                    and hasattr(self, "_validate_deepseek_json_response")
                )

                finish_reason = "stop"
                tool_calls_list = None

                if _json_mode:
                    is_valid, parsed, verr = self._validate_deepseek_json_response(text)
                    max_corr = JSON_API_CONFIG.get("max_corrective_retries", 2)
                    corr = 0
                    while not is_valid and corr < max_corr:
                        corr += 1
                        log.warning(
                            "JSON response invalid (corrective %d/%d): %s | raw[:200]=%s",
                            corr, max_corr, verr, (text or "")[:200],
                        )
                        if '"tool_calls"' in (text or ""):
                            corrective_prompt = (
                                "Your previous reply was not valid JSON. Reply with "
                                "ONE single line of JSON only, no other text, using "
                                "EXACTLY this schema:\n"
                                '{"status":"tool_calls","tool_calls":[{"id":'
                                '"call_<unique_id>","type":"function","function":'
                                '{"name":"<function_name>","arguments":{<args_as_object>}}}]}'
                            )
                        else:
                            corrective_prompt = (
                                "Your previous reply was not valid JSON. Reply with "
                                "ONE single line of JSON only, no other text, using "
                                "EXACTLY this schema:\n"
                                '{"status":"success","choices":[{"index":0,"message":'
                                '{"role":"assistant","content":"<your full answer>"},'
                                '"finish_reason":"stop"}]}'
                            )
                        await asyncio.sleep(ROTATION_CONFIG["retry_delay"])
                        try:
                            # Capture pre-send text for virtual-list change detection.
                            _pre = await self._capture_pre_send_text()
                            _init = await self._count_response_elements()
                            # wrap_as_user_request=False: this IS a system
                            # instruction, do not re-wrap it as a user request.
                            await self.send_prompt(
                                corrective_prompt,
                                mode="continue",
                                wrap_as_user_request=False,
                            )
                            text = await self.wait_for_response(
                                response_selectors=self._response_selectors(),
                                timeout=t["response_wait"],
                                stability_secs=t["stability_check"],
                                stability_polls=t["stability_polls"],
                                poll_interval=t["poll_interval"],
                                initial_response_count=_init,
                                pre_send_text=_pre,
                            )
                        except Exception as corr_exc:  # noqa: BLE001
                            log.warning("Corrective send failed: %s", corr_exc)
                            break
                        is_valid, parsed, verr = self._validate_deepseek_json_response(text)

                    if not is_valid:
                        last_error = f"invalid_json_response: {verr}"
                        log.error(
                            "JSON response still invalid after %d corrective tries", corr
                        )
                        retries += 1
                        await asyncio.sleep(ROTATION_CONFIG["retry_delay"])
                        continue

                    if parsed.get("status") == "error":
                        err_obj = parsed.get("error", {}) or {}
                        last_error = err_obj.get("message") or "model returned error envelope"
                        log.warning("Model returned error envelope: %s", last_error)
                        retries += 1
                        await asyncio.sleep(ROTATION_CONFIG["retry_delay"])
                        continue

                    if parsed.get("status") == "tool_calls":
                        tool_calls_list = parsed.get("tool_calls", [])
                        finish_reason = "tool_calls"
                        cleaned = ""
                        log.info(
                            "JSON response: tool_calls (%d call(s))", len(tool_calls_list)
                        )
                    else:
                        cleaned = parsed["choices"][0]["message"]["content"]
                        finish_reason = parsed["choices"][0].get("finish_reason", "stop")
                else:
                    ok, cleaned = self._validate_response(text)
                    if not ok:
                        cleaned = self._repair_unescaped_quotes(cleaned)
                        cleaned = self._repair_tool_calls_arguments(cleaned)

                # --- Token usage (tiktoken parity with PAF-ModelQwen) --------
                prompt_tokens = _count_tokens(prompt)
                completion_tokens = _count_tokens(cleaned)
                response_time_ms = int((_t_wait - _t0) * 1000)

                # --- x_metadata (parity with PAF-ModelQwen, DeepSeek-flavored)
                try:
                    account_file = str(self._profile_dir_for(self.account))
                except Exception:
                    account_file = ""
                x_metadata = {
                    "model":            self.account,
                    "account_file":     account_file,
                    "account_index":    self._account_index,
                    "timestamp":        int(time.time()),
                    "account_status":   "ok",
                    "retry_count":      retries,
                    "response_time_ms": response_time_ms,
                    "think_mode":       None,  # DeepSeek uses Layer 1/Layer 2, not think_mode
                    "model_tab":        merged_kwargs.get(
                        "model_tab", DEEPSEEK_CONFIG["default_model_tab"]
                    ),
                    "deep_think":       merged_kwargs.get("deep_think", False),
                    "web_search":       merged_kwargs.get("web_search", False),
                }

                result = {
                    "ok": True,
                    "success": True,          # OpenAI-compat alias
                    "finish_reason": finish_reason,
                    "mode": mode,
                    "account": self.account,
                    "text": cleaned,          # backward-compat key
                    "response": cleaned,      # Qwen-style key
                    "code_blocks": self.extract_code_blocks(cleaned),
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                    "x_metadata": x_metadata,
                }
                if tool_calls_list is not None:
                    result["tool_calls"] = tool_calls_list
                return result

            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                log.error("scrape() error (retry %d): %s", retries, exc)
                await self.take_debug_screenshot("scrape_error")
                retries += 1
                await asyncio.sleep(ROTATION_CONFIG["retry_delay"])

        return {
            "ok": False,
            "mode": mode,
            "account": self.account,
            "error": last_error or "unknown error",
            "timestamp": datetime.now().astimezone().isoformat(),
        }

    @abstractmethod
    def _response_selectors(self) -> list[str]:
        """Return the ordered list of selectors used to read AI responses."""
        ...

    def _virtual_list_selectors(self) -> list[str]:
        """
        Return selectors for the virtual-scroll-aware response reader.

        Default implementation reads ``virtual_list_response`` from config
        (added alongside the standard selectors). Subclasses that do not use
        DeepSeek's virtual list can override this to return ``_response_selectors()``.
        """
        try:
            from config import DEEPSEEK_CONFIG
            return DEEPSEEK_CONFIG["selectors"].get(
                "virtual_list_response", self._response_selectors()
            )
        except Exception:
            return self._response_selectors()

    # ------------------------------------------------------------------ #
    # Async context manager
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "BaseAIChatScraper":
        await self.launch_browser(self.account)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close_browser()