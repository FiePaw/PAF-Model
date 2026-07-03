"""
scrapers/deepseek_scraper.py — DeepSeekScraper(BaseAIChatScraper).

Concrete implementation for chat.deepseek.com.

IMPORTANT — DOM verification
----------------------------
DeepSeek is a minified React SPA. Every selector is pulled from config and
marked there with TODO-verify notes. The scraper is intentionally *defensive*:
controls (mode selector, DeepThink/Search tools) are best-effort. When a control
is missing or disabled for the active mode, the scraper logs a warning and
continues rather than crashing — because the available tools DIFFER per mode
(e.g. Search is only available on Instant mode).
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import time
from pathlib import Path
from typing import Optional

import json
import os

from config import AUTH_CONFIG, DEEPSEEK_CONFIG, BROWSER_CONFIG, ROTATION_CONFIG
from scrapers.base_deepseek import BaseAIChatScraper
from scrapers.utils import get_logger

log = get_logger("paf_deepseek.scraper")

_SEL = DEEPSEEK_CONFIG["selectors"]
_T = DEEPSEEK_CONFIG["timeouts"]


class DeepSeekScraper(BaseAIChatScraper):
    """Browser-automation scraper for chat.deepseek.com."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._active_model_tab: str = DEEPSEEK_CONFIG["default_model_tab"]
        # Tracks whether we are inside an active conversation thread.
        # Set to True once a prompt has been sent (or when CONTINUE navigation
        # lands on an existing thread). Reset to False by _goto_new_chat().
        # Mirrors the same flag used by PAF-ModelQwen.
        self._conversation_started: bool = False

        # --- JSON API mode (prompt wrapping) per-request state ------------
        # Set by send_prompt() before building the [SYSTEM CONTEXT]/[USER
        # REQUEST] wrapper. Reset is not required between calls; each request
        # overwrites them (defaults applied when absent).
        self._tools: Optional[list[dict]] = None
        self._max_tokens: Optional[int] = None
        self._system_prompt: Optional[str] = None
        self._last_prompt: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Generic selector helpers
    # ------------------------------------------------------------------ #
    async def _find_first(self, selectors: list[str], timeout_ms: int = 4000):
        """Return the first locator (from a list) that resolves to >=1 element.

        OPTIMISATION (two-phase, total-budget aware):
          Phase 1 (instant): a no-wait existence check across all selectors.
            Covers the common case where the element is already in the DOM and
            returns immediately (no timeout cost at all).
          Phase 2 (bounded wait): only if phase 1 finds nothing. The timeout
            budget is SHARED across the selector candidates instead of being
            spent in full on each one. Previously a list of N selectors where
            none matched cost up to ``timeout_ms * N`` (e.g. 3 selectors x
            2500ms = 7.5s wasted). Now the total wait never exceeds timeout_ms.
        """
        if self.page is None:
            return None

        # --- Phase 1: instant existence check (no waiting) -------------------
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                if await loc.count():
                    return loc.first
            except Exception:
                continue

        # --- Phase 2: bounded wait, budget shared across selectors ----------
        if not selectors:
            return None
        per_selector = max(250, timeout_ms // len(selectors))
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                await loc.first.wait_for(state="attached", timeout=per_selector)
                if await loc.count():
                    return loc.first
            except Exception:
                continue
        return None

    async def _click_first(self, selectors: list[str], timeout_ms: int = 4000) -> bool:
        loc = await self._find_first(selectors, timeout_ms)
        if loc is None:
            return False
        try:
            await loc.click(timeout=timeout_ms)
            await asyncio.sleep(_T["between_actions"])
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Click failed for %s: %s", selectors[0], exc)
            return False

    async def _is_active(self, loc) -> bool:
        """
        Heuristic: is a mode pill / tool toggle currently active?

        Checks (in order):
          1. aria-checked / aria-pressed / aria-selected attributes
          2. Class fragment hint (e.g. "active" substring in class)
          3. Class-count comparison: DeepSeek's active pill gets an EXTRA
             minified class (e.g. _31a22b0) that inactive siblings lack.
             If the element and its sibling(s) can be compared, the one
             with more classes is considered active.
        """
        try:
            cls = (await loc.get_attribute("class")) or ""
            aria_checked  = (await loc.get_attribute("aria-checked"))  or ""
            aria_pressed  = (await loc.get_attribute("aria-pressed"))  or ""
            aria_selected = (await loc.get_attribute("aria-selected")) or ""
            hint = DEEPSEEK_CONFIG["selectors"].get("active_marker_class_hint", "active")

            # Check 1: ARIA state attributes
            if (
                aria_checked.lower()  == "true"
                or aria_pressed.lower()  == "true"
                or aria_selected.lower() == "true"
            ):
                return True

            # Check 2: Class fragment hint
            if hint and hint in cls.lower():
                return True

            return False
        except Exception:
            return False

    async def _is_active_by_class_count(self, loc) -> bool:
        """
        DeepSeek-specific heuristic: the active mode pill has MORE CSS classes
        than its inactive siblings (e.g. active pill has 3 classes, inactive
        has 2). Compare the target element's class count against its siblings.

        Returns True if the element has strictly more classes than at least one
        sibling. Returns False if comparison is impossible (no siblings, no
        class, or any exception).
        """
        try:
            cls = (await loc.get_attribute("class")) or ""
            my_count = len(cls.split())
            if my_count == 0:
                return False

            # Navigate to parent, then check sibling class counts.
            parent = loc.locator('..')
            if await parent.count() == 0:
                return False

            siblings = parent.first.locator('> *')
            sib_count = await siblings.count()
            if sib_count < 2:
                return False  # no siblings to compare

            min_sib_classes = my_count  # start with own count
            for i in range(sib_count):
                sib = siblings.nth(i)
                sib_cls = (await sib.get_attribute("class")) or ""
                sib_len = len(sib_cls.split())
                if sib_len > 0 and sib_len < min_sib_classes:
                    min_sib_classes = sib_len

            if my_count > min_sib_classes:
                log.debug(
                    "Active by class-count heuristic: %d classes vs sibling min %d",
                    my_count, min_sib_classes,
                )
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Layer 1 — Mode selector (Instant / Expert / Vision)
    # ------------------------------------------------------------------ #
    async def _select_model_tab(self, model_tab: str) -> None:
        model_tab = (model_tab or DEEPSEEK_CONFIG["default_model_tab"]).lower()
        tab_selectors = _SEL["model_tab"].get(model_tab)
        if not tab_selectors:
            log.warning("Unknown model_tab '%s'; staying on current tab", model_tab)
            return

        # OPTIMISATION: a brand-new DeepSeek chat always opens on the default
        # mode, and _select_model_tab is only ever called on a fresh chat (NEW
        # mode). When the caller wants that same default mode there is nothing
        # to change -> skip the DOM lookup + active-state checks + click
        # entirely. This is the common "instant" path and saves a full element
        # search every process.
        if model_tab == DEEPSEEK_CONFIG["default_model_tab"]:
            log.debug(
                "Mode '%s' is the default on a fresh chat; skipping tab selection.",
                model_tab,
            )
            self._active_model_tab = model_tab
            return

        # --- Attempt 1: use configured selectors (fast path) -----------------
        loc = await self._find_first(tab_selectors, timeout_ms=_T["element_find"])

        # --- Attempt 2: text-content scan fallback ---------------------------
        # DeepSeek mode pills are plain divs whose only stable attribute is
        # their visible text. The text label is a CHILD div inside the clickable
        # pill parent. We find the text label, then navigate UP to the parent
        # pill (the clickable element that carries the active class).
        if loc is None and self.page is not None:
            label = model_tab.capitalize()  # "Instant" | "Expert" | "Vision"
            try:
                candidates = self.page.get_by_text(label, exact=True)
                count = await candidates.count()
                if count > 0:
                    text_el = candidates.first
                    # Try to click the PARENT of the text label (the pill div),
                    # because the click handler is typically on the pill, not the
                    # text child. Use locator('..') to go up one level.
                    parent = text_el.locator('..')
                    parent_count = await parent.count()
                    if parent_count > 0:
                        loc = parent.first
                        log.debug(
                            "Mode '%s' found via get_by_text→parent fallback "
                            "(%d text match(es))",
                            model_tab, count,
                        )
                    else:
                        loc = text_el
                        log.debug(
                            "Mode '%s' found via get_by_text fallback (no parent) "
                            "(%d text match(es))",
                            model_tab, count,
                        )
            except Exception as exc:  # noqa: BLE001
                log.debug("get_by_text fallback failed for mode '%s': %s", model_tab, exc)

        if loc is None:
            log.warning(
                "Mode '%s' not found in DOM after selector + text-scan fallback. "
                "Continuing on current mode. "
                "(Hint: open DevTools on chat.deepseek.com and inspect the mode pills "
                "to update config.py selectors[model_tab])",
                model_tab,
            )
            return

        # Detect whether the located pill is already the active mode.
        # DeepSeek marks the active pill with an extra minified class (e.g.
        # _31a22b0). Since the class name changes between builds, we compare
        # class count: the active pill has MORE classes than inactive siblings.
        already_active = await self._is_active(loc)
        if not already_active:
            already_active = await self._is_active_by_class_count(loc)

        if already_active:
            log.info("Mode '%s' already active", model_tab)
        else:
            try:
                await loc.click(timeout=3000)
                await asyncio.sleep(_T["between_actions"])
                log.info("Selected mode '%s'", model_tab)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not select mode '%s': %s", model_tab, exc)
                return
        self._active_model_tab = model_tab

    # ------------------------------------------------------------------ #
    # Layer 2 — Tools: DeepThink / Search (availability differs per mode!)
    # ------------------------------------------------------------------ #

    # Internal mapping: tool name (lowercased) -> matrix key in config.
    _TOOL_MATRIX_KEY: dict[str, str] = {
        "deepthink": "deep_think",
        "deep thinking": "deep_think",
        "search": "web_search",
    }

    def _tool_allowed_for_mode(self, name: str) -> bool:
        """
        Check the confirmed tab_toggle_matrix in config to decide whether the
        requested tool is available on the currently-active mode.

        Returns True  → proceed (tool exists for this mode).
        Returns False → skip silently (tool is NOT present for this mode).

        If the matrix has no entry for the active mode (e.g. a future mode), we
        default to True so the scraper still *tries* rather than silently skips.
        """
        matrix = DEEPSEEK_CONFIG.get("tab_toggle_matrix", {})
        mode_entry = matrix.get(self._active_model_tab)
        if mode_entry is None:
            # Unknown mode — don't block, let the DOM check decide.
            return True
        matrix_key = self._TOOL_MATRIX_KEY.get(name.lower())
        if matrix_key is None:
            # Unknown tool name — don't block.
            return True
        return bool(mode_entry.get(matrix_key, False))

    async def _set_toggle(self, name: str, selectors: list[str], desired: bool) -> None:
        """
        Defensively enable a DeepSeek tool (DeepThink or Search). If the tool
        is absent or disabled in the currently-active mode, log accordingly
        and continue (do NOT crash).

        Matrix pre-check: before touching the DOM, consult tab_toggle_matrix to
        see if the tool is even supposed to exist on this mode. If not, skip
        silently (no warning — it's expected behaviour, not an error).
        """
        if not desired:
            return  # only act when caller wants it ON; default state is OFF

        # --- Pre-check: is this tool available on the active mode? ------------
        if not self._tool_allowed_for_mode(name):
            log.debug(
                "'%s' tool is not available on mode '%s' (confirmed by matrix) — "
                "skipping silently.",
                name, self._active_model_tab,
            )
            return

        loc = await self._find_first(selectors, timeout_ms=_T["element_find"])
        if loc is None:
            # Tool should be present (matrix says so) but wasn't found —
            # this is a genuine selector mismatch worth warning about.
            log.warning(
                "'%s' tool not found in DOM for mode '%s'. "
                "Selector may need updating — check config.py selectors. "
                "Continuing without enabling '%s'.",
                name, self._active_model_tab, name,
            )
            return

        # Respect disabled state.
        try:
            disabled = (await loc.get_attribute("aria-disabled")) or ""
            if disabled.lower() == "true" or not await loc.is_enabled():
                log.warning(
                    "'%s' tool is disabled for mode '%s' — skipping.",
                    name, self._active_model_tab,
                )
                return
        except Exception:
            pass

        if await self._is_active(loc):
            log.info("'%s' already ON", name)
            return
        try:
            await loc.click(timeout=2500)
            await asyncio.sleep(_T["between_actions"])
            log.info("Enabled '%s' tool", name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not enable '%s' tool: %s", name, exc)

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    async def _goto_new_chat(self) -> None:
        """
        Navigate to a fresh chat page.

        Strategy (mirrors PAF-ModelQwen):
          1. Try the in-app "New chat" button — keeps the SPA warm, faster.
          2. Fall back to hard page.goto() if the button is absent.
        Always resets _conversation_started so the next send_prompt() knows it
        is starting a brand-new thread.
        """
        if self.page is None:
            return
        # Prefer the in-app New chat button (keeps SPA state warm); fall back to
        # a hard navigation.
        clicked = await self._click_first(_SEL["new_chat_button"], timeout_ms=2500)
        if not clicked:
            await self.page.goto(
                DEEPSEEK_CONFIG["new_chat_url"],
                wait_until="domcontentloaded",
                timeout=_T["page_load"] * 1000,
            )
        await asyncio.sleep(_T["between_actions"])
        # CRITICAL: reset flag so the next send_prompt() treats this as turn 1.
        self._conversation_started = False
        # A fresh chat resets DeepSeek's mode pills back to the default, so any
        # cached "active mode" no longer reflects the page. Clear it so the next
        # _select_model_tab() makes a correct decision.
        self._active_model_tab = None
        log.debug("_goto_new_chat: landed on new-chat page, _conversation_started=False")

    async def _ensure_page_ready(self, mode: str) -> None:
        """
        Single navigation gate — mirrors PAF-ModelQwen exactly.

        NEW  → always call _goto_new_chat() to land on a fresh thread.
        CONTINUE + _conversation_started=True
             → verify we are still on a DeepSeek page (URL sanity check).
               If the page drifted away (e.g. due to a rotation/restart), fall
               back to a new chat rather than silently sending to the wrong page.
        CONTINUE + _conversation_started=False
             → treat as NEW (safe fallback for first turn after a restart).

        NOTE: no authentication checks here — that is handled at a higher
        level (base_scraper.scrape → ensure_authenticated). This keeps the
        method side-effect-free for the CONTINUE path, exactly as Qwen does.
        """
        if mode == "new" or not self._conversation_started:
            await self._goto_new_chat()
        else:
            # CONTINUE: verify the browser is still on a DeepSeek page.
            # If _conversation_started=True but something (rotation, restart,
            # auth redirect) navigated the browser away, we would silently send
            # the prompt to the wrong page — same bug Qwen guards against by
            # always doing the navigation in public.py before calling scrape().
            current_url = self.page.url if self.page else ""
            if not current_url or DEEPSEEK_CONFIG["base_url"] not in current_url:
                log.warning(
                    "_ensure_page_ready: CONTINUE requested but page is at '%s' "
                    "(not a DeepSeek conversation) — opening new chat as fallback.",
                    current_url[:80],
                )
                await self._goto_new_chat()
            else:
                log.debug(
                    "_ensure_page_ready: CONTINUE — page at %s, skipping navigation.",
                    current_url[:80],
                )

    async def _ensure_loaded(self) -> None:
        if self.page is None:
            await self.launch_browser(self.account)
        assert self.page is not None
        if DEEPSEEK_CONFIG["base_url"] not in (self.page.url or ""):
            await self.page.goto(
                DEEPSEEK_CONFIG["base_url"],
                wait_until="domcontentloaded",
                timeout=_T["page_load"] * 1000,
            )
        # Profile-first: reuse the saved session. If the login DOM appears
        # (fresh profile or expired session), ensure_authenticated() logs in.
        await self.ensure_authenticated()

    # ------------------------------------------------------------------ #
    # Authentication — profile-first, email + password fallback
    # ------------------------------------------------------------------ #
    async def ensure_authenticated(self) -> bool:
        """
        Idempotent auth check.

        Flow: if the saved profile session is still valid -> done. If the login
        DOM is showing (fresh profile or expired session) -> log in with the
        email + password from cookies/auth.json for the current account, which
        repopulates the persistent profile. Returns True on success.
        """
        if self.page is None:
            return False

        if not await self.is_session_expired():
            self._authenticated = True
            return True

        log.info("Login DOM detected for account '%s' -> logging in", self.account)
        ok = await self.login()
        self._authenticated = ok
        return ok

    async def login(
        self, email: Optional[str] = None, password: Optional[str] = None
    ) -> bool:
        """
        Perform email + password login on the DeepSeek sign-in page, using the
        credentials resolved for the current account (auth.json / CLI / env).
        On success the persistent profile stores the session for next time.

        If an anti-bot captcha/slider appears, headless login cannot complete —
        re-run once with --no-headless to solve it (the profile remembers it).
        """
        if self.page is None:
            await self.launch_browser(self.account)
        assert self.page is not None

        email, password = self._resolve_credentials(email, password)
        if not email or not password:
            log.error(
                "No credentials for account '%s'. Add it to cookies/auth.json, "
                "pass --email/--password, or set %s / %s env vars.",
                self.account, AUTH_CONFIG["env_email"], AUTH_CONFIG["env_password"],
            )
            return False

        log.info("Logging in to DeepSeek as %s", email)
        await self.page.goto(
            AUTH_CONFIG["login_url"],
            wait_until="domcontentloaded",
            timeout=_T["page_load"] * 1000,
        )
        await asyncio.sleep(_T["between_actions"])

        login_sel = _SEL["login"]
        email_loc = await self._find_first(login_sel["email_input"], timeout_ms=8000)
        pwd_loc = await self._find_first(login_sel["password_input"], timeout_ms=8000)
        if email_loc is None or pwd_loc is None:
            log.error(
                "Login form fields not found (TODO: verify login selectors). "
                "Maybe already logged in?"
            )
            # Maybe we are already in the app.
            return not await self.is_session_expired()

        try:
            await email_loc.click()
            await email_loc.fill(email)
            await pwd_loc.click()
            await pwd_loc.fill(password)
            await asyncio.sleep(_T["between_actions"])
        except Exception as exc:  # noqa: BLE001
            log.error("Failed filling login form: %s", exc)
            return False

        # Optional consent checkbox (best-effort; ignore if absent).
        try:
            cb = await self._find_first(login_sel["agree_checkbox"], timeout_ms=1200)
            if cb is not None and not await cb.is_checked():
                await cb.check(timeout=1500)
        except Exception:
            pass

        # Submit.
        clicked = await self._click_first(login_sel["login_button"], timeout_ms=5000)
        if not clicked:
            log.info("Login button not clickable; pressing Enter")
            await pwd_loc.press("Enter")

        return await self._wait_for_login_result()

    async def _wait_for_login_result(self) -> bool:
        """Poll for login success (chat UI) / failure (captcha or error)."""
        assert self.page is not None
        deadline = asyncio.get_event_loop().time() + AUTH_CONFIG["login_wait"]
        login_sel = _SEL["login"]

        while asyncio.get_event_loop().time() < deadline:
            # Captcha / slider -> cannot proceed headlessly.
            captcha = await self._find_first(login_sel["captcha"], timeout_ms=800)
            if captcha is not None:
                msg = (
                    "Captcha/slider detected during login. Re-run once with "
                    "--no-headless to solve it; the persistent profile will "
                    "remember the session afterwards."
                )
                if AUTH_CONFIG.get("fail_loud_on_captcha", True):
                    log.error(msg)
                    await self.take_debug_screenshot("login_captcha")
                    return False
                log.warning(msg)

            # Success: we left the sign-in page AND the chat input is present.
            on_login = AUTH_CONFIG["login_url"] in (self.page.url or "")
            if not on_login:
                chat = await self._find_first(_SEL["chat_input"], timeout_ms=1500)
                if chat is not None:
                    await asyncio.sleep(AUTH_CONFIG["post_login_settle"])
                    log.info("Login successful — session saved in profile '%s'",
                             self.account)
                    self._authenticated = True
                    # The persistent profile already stores the session. Also
                    # export a cookie backup for debugging (best-effort).
                    try:
                        await self.save_cookies()
                    except Exception:
                        pass
                    return True

            # Inline error (wrong password etc.)
            err = await self._find_first(login_sel["error_message"], timeout_ms=600)
            if err is not None:
                try:
                    txt = (await err.inner_text()).strip()
                except Exception:
                    txt = ""
                if txt:
                    log.error("Login error: %s", txt)
                    await self.take_debug_screenshot("login_error")
                    return False

            await asyncio.sleep(1.0)

        log.error("Login timed out after %ss", AUTH_CONFIG["login_wait"])
        await self.take_debug_screenshot("login_timeout")
        return False

    # ------------------------------------------------------------------ #
    # send_prompt
    # ------------------------------------------------------------------ #
    async def send_prompt(
        self,
        prompt: str,
        mode: str = "new",
        model_tab: str = "instant",
        deep_think: bool = False,
        web_search: bool = False,
        attachments: Optional[list[str | Path]] = None,
        wrap_as_user_request: bool = True,
        tools: Optional[list[dict]] = None,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        **kwargs,
    ) -> str:
        """
        Send a prompt to DeepSeek.

        Order:
          _ensure_page_ready(mode)      ← sole navigation gate (Qwen-style)
          [NEW only] select mode tab    ← Instant / Expert / Vision
          [NEW only] enable tools       ← DeepThink / Search
          attach files (both modes)
          type prompt → send
        Returns the response selector string consumed by wait_for_response().

        FIX: _ensure_loaded() has been intentionally removed from here.
        Calling it inside send_prompt() introduced a second authentication check
        that could call login() → page.goto(login_url) AFTER the caller already
        navigated to a conversation URL, silently destroying the CONTINUE context.
        Qwen's send_prompt() does not call any auth/load helper — it only calls
        _ensure_page_ready(). We follow the same contract:
          • Initial load / auth → ChatClient.launch() or base_scraper.scrape()
          • Navigation gate    → _ensure_page_ready() (this function, line below)
        """
        # --- TIMING instrumentation (sub-stages of send_prompt) -------------
        _s0 = time.monotonic()

        # --- JSON API mode: wrap prompt in [SYSTEM CONTEXT]/[USER REQUEST] ---
        # Store per-request state so _build_wrapped_prompt() can embed tools,
        # max_tokens, and any client system prompt. Corrective-feedback sends
        # pass wrap_as_user_request=False (the message is already an
        # instruction, not a new user request) so they are sent verbatim.
        self._tools = tools
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._last_prompt = prompt

        from config import JSON_API_CONFIG  # keep base import-light
        _json_mode = JSON_API_CONFIG.get("enabled", False)
        outgoing_prompt = (
            self._build_wrapped_prompt(prompt)
            if (_json_mode and wrap_as_user_request)
            else prompt
        )

        # Sole navigation gate — decides new chat vs continue existing thread.
        await self._ensure_page_ready(mode)
        _s_nav = time.monotonic()

        if mode == "new":
            # Select mode first — determines which tools are available.
            await self._select_model_tab(model_tab)
            # Enable tools — checked AFTER mode selection (availability differs per mode).
            await self._set_toggle("DeepThink", _SEL["deep_think_toggle"], deep_think)
            await self._set_toggle("Search", _SEL["web_search_toggle"], web_search)
        else:
            # CONTINUE mode: conversation is already in progress.
            # DeepSeek hides the mode pills and tool toggles once a conversation
            # has started — they are only shown on a fresh new-chat page.
            # Attempting _select_model_tab() / _set_toggle() here would time out
            # waiting for DOM elements that don't exist.
            # The mode and tools were set at the start of the conversation and
            # cannot be changed mid-thread; skip silently.
            log.debug(
                "CONTINUE mode: skipping mode/tool selection "
                "(controls are hidden in an active conversation)"
            )

        _s_ctrl = time.monotonic()

        # Attachments (image/doc) via clipboard paste.
        if attachments:
            for att in attachments:
                await self._attach_via_clipboard(att)

        # Type prompt.
        input_loc = await self._find_first(_SEL["chat_input"], timeout_ms=8000)
        if input_loc is None:
            raise RuntimeError(
                "Chat input not found (TODO: verify #chat-input selector)."
            )
        await input_loc.click()
        # Clear any existing content, then set the full prompt via fill().
        #
        # CRITICAL: Use fill() — NOT type() — to insert the prompt.
        # type() simulates per-character keypresses; newline characters
        # (\n) in multi-line prompts are interpreted as Enter key presses,
        # which prematurely submits the chat form and truncates the prompt
        # at the first newline. fill() sets the value programmatically via
        # the DOM without firing per-character key events, so newlines are
        # inserted as literal text in the textarea. (Same approach as
        # PAF-ModelQwen's send_prompt.)
        await input_loc.fill("")
        await input_loc.fill(outgoing_prompt)
        await asyncio.sleep(_T["between_actions"])
        _s_type = time.monotonic()

        # Send: prefer clicking the send button; fall back to Enter.
        sent = await self._click_first(_SEL["send_button"], timeout_ms=4000)
        if not sent:
            log.info("Send button not clickable; pressing Enter as fallback")
            await input_loc.press("Enter")

        # Mark conversation as active so future CONTINUE calls skip navigation.
        self._conversation_started = True

        # One-line breakdown of where send_prompt spent its time.
        log.info(
            "[TIMING] send_prompt: nav=%.2fs controls=%.2fs type=%.2fs "
            "send=%.2fs (mode=%s)",
            _s_nav - _s0,
            _s_ctrl - _s_nav,
            _s_type - _s_ctrl,
            time.monotonic() - _s_type,
            mode,
        )

        # Return the selector wait_for_response should poll.
        return _SEL["assistant_message"][0]

    # ------------------------------------------------------------------ #
    # Turn 2: tool-result injection (mirrors PAF-ModelQwen)
    # ------------------------------------------------------------------ #
    def _build_tool_result_prompt(
        self,
        tool_messages: list[dict],
        next_user_msg: Optional[str] = None,
    ) -> str:
        """
        Build prompt for Turn 2 (inject tool result into existing DeepSeek conversation).

        Format sent to DeepSeek (CONTINUE mode):
            [TOOL RESULT]
            {"tool_call_id":"call_001","name":"write_file","result":{"success":true}}

            [USER REQUEST]
            {"continue":true,"model":"account1"}

        If next_user_msg is provided (user sends a new message after tool result):
            [USER REQUEST]
            {"prompt":"now run it","model":"account1"}
        """
        account_name = self.account or "deepseek"
        parts = ["[TOOL RESULT]"]

        for tm in tool_messages:
            entry = {
                "tool_call_id": tm.get("tool_call_id"),
                "name":         tm.get("name"),
                "result":       tm.get("content"),
            }
            parts.append(json.dumps(entry, ensure_ascii=False))

        user_request: dict = {"model": account_name}
        if next_user_msg:
            user_request["prompt"] = next_user_msg
        else:
            user_request["continue"] = True   # signal: no new user message
        if self._max_tokens is not None:
            user_request["max_tokens"] = self._max_tokens

        parts += ["", "[USER REQUEST]", json.dumps(user_request, ensure_ascii=False)]
        return "\n".join(parts)

    async def scrape_with_tool_result(
        self,
        tool_messages: list[dict],
        next_user_msg: Optional[str] = None,
    ) -> dict:
        """
        Send tool result to DeepSeek in CONTINUE session (Turn 2).
        Called by public.py after the client executes a tool locally.

        Args:
            tool_messages: list of {role:tool, tool_call_id, name, content} dicts
            next_user_msg: optional next user message after tool result

        Returns:
            dict with finish_reason and possibly tool_calls (if DeepSeek requests
            another tool) or response (stop). No corrective loop — if the
            response is invalid JSON, returns error immediately (same as Qwen).
        """
        from config import DEEPSEEK_CONFIG
        from datetime import datetime

        prompt = self._build_tool_result_prompt(tool_messages, next_user_msg)

        # ── Send + wait for response (same pattern as scrape()) ──────────── #
        # send_prompt() returns a selector string, NOT the response text.
        # We must call wait_for_response() separately to get the actual text.
        # For CONTINUE mode: capture pre_send_text BEFORE sending so the
        # text-change detection knows when the new response arrives.
        # ──────────────────────────────────────────────────────────────────── #
        if self.page is None:
            await self.launch_browser(self.account)

        pre_send_text = await self._capture_pre_send_text()
        initial_response_count = await self._count_response_elements()

        # wrap_as_user_request=False: prompt is already a fully structured
        # [TOOL RESULT]/[USER REQUEST] envelope — do NOT re-wrap it.
        # mode="continue" required: tool result must go to the same session.
        await self.send_prompt(
            prompt, mode="continue", wrap_as_user_request=False
        )

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

        # Validate response — no corrective loop (Qwen pattern).
        is_valid, parsed, err = self._validate_deepseek_json_response(text)
        if is_valid and parsed:
            status = parsed.get("status")
            if status == "tool_calls":
                return {
                    "ok":            True,
                    "success":       True,
                    "finish_reason": "tool_calls",
                    "mode":          "continue",
                    "account":       self.account,
                    "tool_calls":    parsed.get("tool_calls", []),
                }
            if status == "success":
                content = parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {
                    "ok":            True,
                    "success":       True,
                    "finish_reason": "stop",
                    "mode":          "continue",
                    "account":       self.account,
                    "text":          content,
                    "response":      content,
                }
            if status == "error":
                err_obj = parsed.get("error", {}) or {}
                return {
                    "ok":      False,
                    "success": False,
                    "mode":    "continue",
                    "account": self.account,
                    "error":   err_obj.get("message") or "model error envelope",
                }
        # Invalid JSON — return error immediately, no retry (Qwen pattern)
        log.warning(
            "tool-result JSON invalid: %s | raw[:200]=%s",
            err, (text or "")[:200],
        )
        return {
            "ok":      False,
            "success": False,
            "mode":    "continue",
            "account": self.account,
            "error":   f"Invalid response after tool result: {err}",
            "raw":     text,
        }
    # ------------------------------------------------------------------ #
    # Attachments — CDP clipboard inject + Ctrl+V (NOT <input type=file>)
    # ------------------------------------------------------------------ #
    async def _attach_via_clipboard(self, file_path: str | Path) -> None:
        """
        Inject an image into the clipboard via the page context and paste it
        into the chat box with Ctrl+V. Many modern chat UIs (DeepSeek included)
        accept paste events but hide the native <input type=file>.

        NOTE: DeepSeek image input is associated with the Vision tab. Verify
        supported types (config.attachments.supported_types) before relying on
        non-image uploads — PDF/doc support is unconfirmed.
        """
        p = Path(file_path)
        if not p.exists():
            log.warning("Attachment not found: %s", p)
            return

        mime, _ = mimetypes.guess_type(str(p))
        mime = mime or "application/octet-stream"
        supported = DEEPSEEK_CONFIG["attachments"]["supported_types"]
        if mime not in supported:
            log.warning(
                "Attachment type '%s' may be unsupported by DeepSeek "
                "(supported: %s). Attempting anyway.",
                mime, supported,
            )

        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        # Build a File from base64 in the page and dispatch a paste event with
        # the file in clipboardData. This avoids needing OS clipboard access.
        js = """
        async ([b64, mime, name]) => {
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const file = new File([bytes], name, {type: mime});
            const dt = new DataTransfer();
            dt.items.add(file);
            const target = document.querySelector('textarea#chat-input')
                || document.querySelector('div[contenteditable="true"]')
                || document.body;
            const evt = new ClipboardEvent('paste', {
                bubbles: true, cancelable: true, clipboardData: dt
            });
            target.dispatchEvent(evt);
            return true;
        }
        """
        try:
            await self.page.evaluate(js, [b64, mime, p.name])
            await asyncio.sleep(_T["between_actions"] * 2)
            log.info("Pasted attachment via clipboard: %s", p.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Clipboard paste failed for %s: %s", p.name, exc)

    # ------------------------------------------------------------------ #
    # State checks
    # ------------------------------------------------------------------ #
    async def is_rate_limited(self) -> bool:
        if self.page is None:
            return False
        try:
            body = (await self.page.inner_text("body")).lower()
        except Exception:
            return False
        return any(
            phrase in body for phrase in ROTATION_CONFIG["rate_limit_phrases"]
        )

    async def is_session_expired(self) -> bool:
        """
        Fast check: are we on / showing the login DOM?

        FIX: removed 'div:has-text("Log in")' from the login-form selector
        check. Playwright's :has-text() matches ANY element whose subtree
        contains the text — so on a logged-in conversation page the sidebar
        "Log in" link, a referral banner, or any help text would trigger a
        false positive, causing ensure_authenticated() → login() to navigate
        away from the conversation URL and break CONTINUE mode.

        Detection order (cheapest → safest):
          1. URL is the sign-in URL             → definitely expired
          2. input[type="password"] is VISIBLE  → login form showing
          3. Chat input present                 → definitely authenticated
          4. Body contains session-expiry phrase → expired
        """
        if self.page is None:
            return False

        # 1) On the sign-in page URL (instant, no DOM query needed).
        if DEEPSEEK_CONFIG["login_url"] in (self.page.url or ""):
            return True

        # 2) Login form: only check for a VISIBLE password field.
        #    This is far more specific than :has-text() and will not fire on
        #    logged-in pages that happen to mention "Log in" somewhere.
        try:
            pwd = await self.page.query_selector('input[type="password"]')
            if pwd is not None and await pwd.is_visible():
                return True
        except Exception:
            pass

        # 3) Chat input present → definitely authenticated (fast exit).
        for sel in _SEL["chat_input"]:
            try:
                if await self.page.query_selector(sel) is not None:
                    return False
            except Exception:
                continue

        # 4) Phrase match on body text (last resort).
        try:
            body = (await self.page.inner_text("body")).lower()
        except Exception:
            return False
        return any(
            phrase in body
            for phrase in ROTATION_CONFIG["session_expired_phrases"]
        )

    # ------------------------------------------------------------------ #
    # localStorage seeding (optional SPA tokens)
    # ------------------------------------------------------------------ #
    def _local_storage_items(self) -> dict[str, str]:
        """
        Optional localStorage seeding. The persistent profile normally captures
        everything after the first login, so this is rarely needed. If you find
        auth-adjacent localStorage keys (DevTools -> Application -> Local Storage
        on chat.deepseek.com), drop them in cookies/<account>.localstorage.json
        and they'll be injected before first page load.
        TODO: verify which localStorage keys (if any) DeepSeek requires.
        """
        from config import COOKIES_DIR
        sidecar = COOKIES_DIR / f"{self.account}.localstorage.json"
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                return {str(k): str(v) for k, v in data.items()}
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed reading localStorage sidecar: %s", exc)
        return {}

    # ------------------------------------------------------------------ #
    # Rotation / restart overrides — reset conversation state
    # ------------------------------------------------------------------ #
    async def _rotate_account(self, restart_first: bool = True) -> bool:
        """
        Override: reset _conversation_started after rotating.

        After rotation the browser lands on the new account's home page, NOT
        on the previous conversation URL. If we kept _conversation_started=True,
        _ensure_page_ready("continue") would skip navigation and the next prompt
        would be sent to the home page, silently starting a new conversation.

        Mirrors the implicit reset in Qwen: _rotate_account() always calls
        launch_browser / close/reopen, which navigates to the home page; Qwen's
        next call to _ensure_page_ready("new") or scrape(mode="new") then calls
        _goto_new_chat() which resets the flag explicitly.
        """
        result = await super()._rotate_account(restart_first=restart_first)
        if result:
            self._conversation_started = False
            log.debug(
                "_rotate_account: account rotated — _conversation_started reset to False"
            )
        return result

    async def restart_browser(self, account=None) -> "Page":
        """
        Override: reset _conversation_started on browser restart.

        After a restart the page is fresh (home or about:blank). Keeping the
        flag True would cause the same wrong-page bug as after rotation.
        """
        self._conversation_started = False
        log.debug(
            "restart_browser: restarting — _conversation_started reset to False"
        )
        return await super().restart_browser(account)

    # ------------------------------------------------------------------ #
    # Validation / hooks
    # ------------------------------------------------------------------ #
    def _validate_response(self, raw: str) -> tuple[bool, str]:
        return self._validate_deepseek_response(raw)

    def _validate_deepseek_response(self, raw: str) -> tuple[bool, str]:
        """
        Validate a DeepSeek response before parsing. DeepSeek returns rendered
        markdown text (not a JSON envelope), so validation is lenient: non-empty
        text that is not obviously an error/limit notice is considered OK.
        """
        if not raw or not raw.strip():
            return False, ""
        low = raw.lower()
        for phrase in ROTATION_CONFIG["rate_limit_phrases"]:
            if phrase in low:
                return False, raw
        return True, raw.strip()

    # ------------------------------------------------------------------ #
    # JSON API mode — prompt wrapping + structured-response validation
    # (parity with PAF-ModelQwen's _build_wrapped_prompt /
    #  _validate_qwen_response). Only active when JSON_API_CONFIG["enabled"].
    # ------------------------------------------------------------------ #
    def _build_wrapped_prompt(self, prompt: str) -> str:
        """
        Wrap a user prompt into the [SYSTEM CONTEXT] / [USER REQUEST] envelope.

        Plain chat (no tools):
            [SYSTEM CONTEXT]
            You are operating as a JSON API endpoint ...
            (schema for {"status":"success","choices":[...]})

            [USER REQUEST]
            {"prompt": "...", "model": "<account>"}

        Tool calling (tools present):
            [SYSTEM CONTEXT]
            You are a pure JSON API endpoint ...
            AVAILABLE FUNCTIONS: [ ... ]
            (schema for tool_calls OR success)

            [USER REQUEST]
            {"prompt": "...", "model": "<account>", "max_tokens": N}

        The wrapper is what makes DeepSeek emit a parseable envelope instead of
        free-form markdown. Mirrors qwen_scraper._build_wrapped_prompt.
        """
        from config import JSON_API_CONFIG

        account_name = self.account or "deepseek"
        user_request: dict = {"prompt": prompt, "model": account_name}
        if self._max_tokens is not None:
            user_request["max_tokens"] = self._max_tokens

        parts: list[str] = ["[SYSTEM CONTEXT]"]

        # Optional client-supplied system prompt is prepended verbatim.
        if self._system_prompt:
            parts += [self._system_prompt.strip(), ""]

        if self._tools:
            # ── Tool-calling mode ───────────────────────────────────────
            # Avoid the literal word that triggers DeepSeek's internal tool
            # registry; describe them as client-side "functions" instead.
            tool_names = [
                t.get("function", {}).get("name", "")
                for t in self._tools
                if t.get("function", {}).get("name")
            ]
            parts += [
                "You are a pure JSON API endpoint. You do NOT have any built-in",
                "capabilities (no web search, no code execution, no file access,",
                "no image generation). You are a stateless text-in / JSON-out",
                "processor.",
                "",
                "The client system has its own external executor that can run",
                "the following named functions on your behalf:",
                "",
                "AVAILABLE FUNCTIONS (client-side, executed externally):",
                json.dumps(self._tools, ensure_ascii=False, indent=2),
                "",
                f"These are the ONLY function names you may request: {tool_names}",
                "Do NOT invent or use any other function name.",
                "",
                "RESPONSE FORMAT — choose exactly ONE, ONE line of JSON only:",
                "",
                "A) If you need the client to execute a function before you can answer:",
                '{"status":"tool_calls","tool_calls":[{"id":"call_<unique_id>",'
                '"type":"function","function":{"name":"<function_name>",'
                '"arguments":{<args_as_object>}}}]}',
                "",
                "B) If you have enough information to give a final answer:",
                '{"status":"success","choices":[{"index":0,"message":{"role":'
                '"assistant","content":"<full_answer>"},"finish_reason":"stop"}]}',
                "",
                "RULES:",
                "- Output ONLY raw JSON. No markdown, no explanation, no extra text.",
                "- arguments MUST be a JSON object (dict), NOT a string.",
                "- id MUST be unique per call, format: call_<number_or_letters>.",
                "- Do NOT add any field outside the schemas above.",
                f"- Only use function names from this list: {tool_names}.",
            ]
        else:
            # ── Plain chat mode ─────────────────────────────────────────
            parts.append(JSON_API_CONFIG["chat_system_instruction"])

        parts += ["", "[USER REQUEST]", json.dumps(user_request, ensure_ascii=False)]
        return "\n".join(parts)

    def _validate_deepseek_json_response(
        self, raw: str
    ) -> "tuple[bool, dict | None, str]":
        """
        Validate that a DeepSeek response is a JSON envelope with one of:
          success:    {"status":"success","choices":[{"index":0,"message":
                       {"role":"assistant","content":"..."},"finish_reason":"stop"}]}
          tool_calls: {"status":"tool_calls","tool_calls":[ ... ]}
          error:      {"status":"error","error":{"type":"...","message":"..."}}

        Returns (is_valid, parsed_dict | None, error_reason).

        DeepSeek often renders the JSON with surrounding markdown/code-fences or
        unescaped quotes; this method strips fences and applies the inherited
        repair helpers (_repair_tool_calls_arguments / _repair_unescaped_quotes)
        before giving up.
        """
        if not raw or not raw.strip():
            return False, None, "empty response"

        candidate = self._strip_json_fences(raw.strip())

        def _try_parse(s: str):
            try:
                return json.loads(s), None
            except json.JSONDecodeError as exc:
                return None, str(exc)

        data, err = _try_parse(candidate)
        if data is None:
            repaired = None
            # Repair pass 0: fix invalid backslash escapes (e.g. Windows paths
            #                "C:\Users\..." → "C:\\Users\\...").  Applied first
            #                because \X errors block all subsequent parsing.
            bs_fixed = self._fix_invalid_backslashes(candidate)
            if bs_fixed:
                data, err = _try_parse(bs_fixed)
                if data is not None:
                    candidate = bs_fixed  # use fixed version for downstream
            # Repair pass 0.5: fix missing ']' for choices array
            #   DeepSeek sometimes outputs '}}' instead of '}]}', leaving the
            #   choices array unclosed. Applied before content repairs.
            if data is None:
                bracket_fixed = self._repair_unbalanced_brackets(candidate)
                if bracket_fixed:
                    data, err = _try_parse(bracket_fixed)
                    if data is not None:
                        candidate = bracket_fixed
            if data is None:
                # Repair pass 1: tool_calls arguments (unescaped inner quotes)
                if '"tool_calls"' in candidate or '"arguments"' in candidate:
                    repaired = self._repair_tool_calls_arguments(candidate)
                # Repair pass 2: unescaped quotes in content (legacy heuristic)
                if repaired is None:
                    repaired = self._repair_unescaped_quotes(candidate)
                # Repair pass 3: robust content-field repair (unbalanced braces,
                #                unescaped quotes, literal newlines, near/hard truncation)
                if repaired is None:
                    repaired = self._repair_content_field(candidate)
                # Repair pass 4: regex-based content extraction (absolute last
                #                resort — reconstructs clean JSON from Python dict,
                #                works for any truncation or structural issue)
                if repaired is None:
                    repaired = self._repair_by_regex(candidate)
                if repaired is not None and repaired != candidate:
                    data, err2 = _try_parse(repaired)
                    if data is None:
                        return False, None, f"JSON parse error: {err} | repair failed: {err2}"
                else:
                    return False, None, f"JSON parse error: {err}"

        if not isinstance(data, dict):
            return False, None, "top-level JSON is not an object"

        status = data.get("status")
        if status == "tool_calls":
            tcs = data.get("tool_calls")
            if not isinstance(tcs, list) or not tcs:
                return False, None, "status=tool_calls but tool_calls missing/empty"
            # Normalize: arguments must be a JSON-encoded string (OpenAI spec).
            # DeepSeek sometimes outputs arguments as a raw dict object.
            for tc in tcs:
                func = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(func, dict):
                    args = func.get("arguments")
                    if isinstance(args, dict):
                        func["arguments"] = json.dumps(args, ensure_ascii=False)
            return True, data, ""
        if status == "error":
            # A well-formed error envelope is "valid" JSON; the caller decides.
            return True, data, ""
        if status == "success":
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                return False, None, "status=success but choices missing/empty"
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), str):
                return False, None, "choices[0].message.content missing or not a string"
            return True, data, ""

        return False, None, f"unknown or missing status: {status!r}"

    @staticmethod
    def _strip_json_fences(text: str) -> str:
        """Remove leading/trailing markdown code fences around a JSON payload."""
        t = text.strip()
        if t.startswith("```"):
            # drop opening fence line (``` or ```json) and trailing fence
            first_nl = t.find("\n")
            if first_nl != -1:
                t = t[first_nl + 1:]
            if t.rstrip().endswith("```"):
                t = t.rstrip()[: -3]
        return t.strip()

    def _extra_send_kwargs(self) -> dict:
        return {
            "model_tab": DEEPSEEK_CONFIG["default_model_tab"],
            "deep_think": DEEPSEEK_CONFIG["deep_think_default"],
            "web_search": DEEPSEEK_CONFIG["web_search_default"],
        }

    def _response_selectors(self) -> list[str]:
        # Prefer the explicit assistant message, fall back to the container.
        return _SEL["assistant_message"] + _SEL["response_container"]