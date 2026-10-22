#!/usr/bin/env python3
"""
debug_prompt_qwen.py — Diagnostik DOM & Selector untuk halaman Chat Prompt Qwen.

Tujuan:
  Membantu membedah, menguji, dan memperbarui selector DOM pada halaman prompt
  (input & output) Qwen (chat.qwen.ai). Mendukung dua mode operasi:
    • NEW mode      — Kirim prompt ke chat baru, verifikasi setiap selector I/O.
    • CONTINUE mode — Lanjutkan percakapan yang sama, verifikasi selector pada
                      turn ke-2 (perbedaan DOM antara chat baru vs. lanjutan).

  Ini BUKAN debug login (lihat debug_login_qwen.py untuk itu). Script ini
  mengasumsikan sesi sudah valid (sudah login). Jika belum login, script akan
  mendeteksi dan menginstruksikan user untuk login terlebih dahulu.

Fitur:
  1. Verifikasi Landing Page & status login (reuse pola debug_login_qwen.py).
  2. Pemindaian DOM menyeluruh: textarea, send button, stop button, think-mode
     trigger/dropdown, response containers, loading indicators, toolbar menu,
     new chat button, attachment preview area.
  3. Menguji selector yang terdaftar di config/qwen.py & qwen_scraper.py vs
     DOM langsung, menyarankan kandidat selector baru jika lama gagal.
  4. Menguji INPUT flow: menemukan textarea → fill prompt → klik send → verifikasi
     prompt terkirim.
  5. Menguji OUTPUT flow: menunggu response muncul → verifikasi selector
     response container / is_generating / extract_last_response.
  6. NEW mode: buka chat baru, kirim prompt, verifikasi I/O.
  7. CONTINUE mode: kirim prompt ke-2 di chat yang sama, verifikasi I/O pada
     turn lanjutan (DOM mungkin berbeda: think-mode trigger hilang, response
     container count berubah, dll).
  8. Screenshot & HTML dump di setiap tahap ke folder debug/.
  9. Mode interaktif (--interactive): browser tetap terbuka untuk inspeksi DevTools.
 10. Auto-summary: laporan Markdown & JSON di debug/summary_prompt/.

Penggunaan:
  # Default: visible browser, akun account1, test NEW + CONTINUE
  python debug_prompt_qwen.py

  # Headless
  python debug_prompt_qwen.py --headless

  # Akun spesifik
  python debug_prompt_qwen.py --account account2

  # Hanya scan DOM tanpa mengirim prompt
  python debug_prompt_qwen.py --skip-send

  # Custom prompt
  python debug_prompt_qwen.py --prompt "Explain quantum computing in 2 sentences"

  # Skip CONTINUE mode (hanya NEW)
  python debug_prompt_qwen.py --skip-continue

  # Profil bersih (tanpa cookie/session lama — butuh login dulu)
  python debug_prompt_qwen.py --clean-profile

  # Mode interaktif (tahan browser di setiap step)
  python debug_prompt_qwen.py --interactive
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Pastikan root direktori ada di sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Handle Windows cp1252 console encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.async_api import BrowserContext, Page, async_playwright

from config import (
    BROWSER_CONFIG,
    DEBUG_DIR,
    PROFILES_DIR,
    QWEN_AUTH_CONFIG,
    QWEN_CONFIG,
    QWEN_LAUNCH_ARGS,
)
from scrapers.utils import AuthStore, setup_logger

logger = setup_logger("debug_prompt_qwen")


# ═══════════════════════════════════════════════════════════════════════════════
# Selector Registry — all selectors used by QwenScraper for prompt I/O
# ═══════════════════════════════════════════════════════════════════════════════
# These are extracted from qwen_scraper.py and config/qwen.py so we test
# exactly what the production scraper uses.

SELECTOR_REGISTRY = {
    # ── Input field (textarea / contenteditable) ────────────────────────────
    "input_textarea": {
        "description": "Chat input field where user types prompts",
        "source": "qwen_scraper.py::_find_input()",
        "selectors": [
            "textarea.message-input-textarea",
            "textarea[placeholder]",
            "textarea#chat-input",
            "textarea[data-testid='chat-input']",
            "div[contenteditable='true'][data-testid]",
            "div[contenteditable='true']",
            "textarea",
        ],
    },
    "input_textarea_config": {
        "description": "Chat input from config/qwen.py",
        "source": "config/qwen.py::selectors.prompt_textarea",
        "selectors": [s.strip() for s in QWEN_CONFIG["selectors"]["prompt_textarea"].split(",")],
    },

    # ── Send button ────────────────────────────────────────────────────────
    "send_button": {
        "description": "Send / submit button to dispatch the prompt",
        "source": "qwen_scraper.py::_SEL_SEND_BTN + _find_send_button()",
        "selectors": [
            "button.send-button:not(.disabled)",
            "button.send-button",
            "button[aria-label='Send']:not([disabled])",
            "button[aria-label='Send']",
            "button[aria-label*='send' i]",
            "button[class*='send' i]",
            "button[data-testid*='send' i]",
            "button[class*='ant-btn'][class*='primary']:not([disabled])",
            "button[class*='submit' i]",
            "button[type='submit']",
            "button[data-testid='send-button']",
            "button[aria-label='Send message']",
        ],
    },
    "send_button_config": {
        "description": "Send button from config/qwen.py",
        "source": "config/qwen.py::selectors.send_button",
        "selectors": [s.strip() for s in QWEN_CONFIG["selectors"]["send_button"].split(",")],
    },

    # ── Stop / abort button (visible during generation) ───────────────────
    "stop_button": {
        "description": "Stop generation button (visible while AI is responding)",
        "source": "qwen_scraper.py::_is_generating() + config",
        "selectors": [
            "button.stop-btn",
            'button[class*="stop"]',
            'button[aria-label*="stop" i]',
            'button[title*="stop" i]',
            '[class*="stop-btn"]',
            '[class*="abort"]',
        ],
    },
    "stop_button_config": {
        "description": "Stop button from config/qwen.py",
        "source": "config/qwen.py::selectors.stop_button",
        "selectors": [s.strip() for s in QWEN_CONFIG["selectors"]["stop_button"].split(",")],
    },

    # ── Response extraction ────────────────────────────────────────────────
    "response_container": {
        "description": "Response message containers (used by _extract_last_response)",
        "source": "qwen_scraper.py::_extract_last_response()",
        "selectors": [
            ".qwen-markdown",
            ".qwen-markdown-loose",
            ".chat-response-message",
            ".chat-message-container .chat-response-message",
            '[class*="response-message"]',
            '[class*="chat-response"]',
            '[class*="assistant-message"]',
            '[data-role="assistant"]',
            '[data-author="assistant"]',
        ],
    },
    "response_container_config": {
        "description": "Response container from config/qwen.py",
        "source": "config/qwen.py::selectors.response_container",
        "selectors": [s.strip() for s in QWEN_CONFIG["selectors"]["response_container"].split(",")],
    },

    # ── Streaming / loading indicators ────────────────────────────────────
    "streaming_indicators": {
        "description": "Indicators that AI is still generating (stop btn + CSS classes)",
        "source": "qwen_scraper.py::_is_generating()",
        "selectors": [
            '[class*="streaming"]',
            '[class*="typing"]',
            '[class*="loading-dots"]',
            '[class*="thinking-indicator"]',
            '[class*="cursor-blink"]',
            '.result-streaming',
        ],
    },
    "loading_indicator_config": {
        "description": "Loading indicator from config/qwen.py",
        "source": "config/qwen.py::selectors.loading_indicator",
        "selectors": [s.strip() for s in QWEN_CONFIG["selectors"]["loading_indicator"].split(",")],
    },

    # ── Think mode UI ─────────────────────────────────────────────────────
    "think_mode_trigger": {
        "description": "Think mode dropdown trigger / label",
        "source": "qwen_scraper.py::_set_think_mode()",
        "selectors": [
            ".qwen-thinking-selector",
            ".qwen-chat-v2-dropdown-menu-trigger",
            ".qwen-thinking-selector .qwen-chat-v2-dropdown-menu-trigger",
            ".qwen-select-thinking-label",
            "[class*='thinking-label']",
            "[class*='think-mode']",
            "[class*='qwen-select']",
        ],
    },
    "think_mode_selected": {
        "description": "Currently selected think mode label",
        "source": "qwen_scraper.py::_get_current_think_mode()",
        "selectors": [
            ".qwen-chat-v2-dropdown-menu-select-label",
            ".qwen-thinking-selector",
            QWEN_CONFIG["selectors"]["think_mode_selected"],
            QWEN_CONFIG["selectors"]["think_mode_trigger"],
            "[class*='thinking-label']",
            "[class*='think-mode-label']",
            "[class*='selected-label']",
        ],
    },
    "think_mode_options": {
        "description": "Think mode dropdown option list (rc-virtual-list & qwen-chat-v2)",
        "source": "qwen_scraper.py::_set_think_mode()",
        "selectors": [
            ".qwen-chat-v2-dropdown-menu li",
            ".qwen-chat-v2-dropdown-menu-item",
            ".rc-virtual-list-holder-inner > *",
            ".rc-virtual-list-holder-inner li",
            QWEN_CONFIG["selectors"]["think_mode_options"],
        ],
    },

    # ── New chat button ───────────────────────────────────────────────────
    "new_chat_button": {
        "description": "Button to start a new chat conversation",
        "source": "config/qwen.py::selectors.new_chat_button",
        "selectors": [s.strip() for s in QWEN_CONFIG["selectors"]["new_chat_button"].split(",")],
    },

    # ── Toolbar menu (mode-select-open for Create Image / Video / Web Search)
    "toolbar_menu": {
        "description": "Toolbar menu trigger (mode-select-open) for task type selection",
        "source": "qwen_scraper.py::_open_toolbar_menu()",
        "selectors": [
            ".mode-select-open",
            "[class*='mode-select-open']",
            "button.mode-select-open",
            "span.mode-select-open",
            "div.mode-select-open",
            "button[class*='mode-select']",
            "[class*='mode-select-btn']",
            "[class*='mode-select-trigger']",
        ],
    },

    # ── Response count (used by _count_response_elements) ─────────────────
    "response_count": {
        "description": "Combined selector for counting response elements (pre/post send)",
        "source": "qwen_scraper.py::_count_response_elements()",
        "selectors": [
            ".chat-message-container .chat-response-message",
            ".qwen-markdown-loose",
            ".qwen-markdown",
        ],
    },

    # ── Attachment preview (after paste/upload) ───────────────────────────
    "attachment_preview": {
        "description": "Attachment preview/thumbnail after upload",
        "source": "qwen_scraper.py::_wait_attachment_preview()",
        "selectors": [
            "[class*='attachment']",
            "[class*='file-preview']",
            "[class*='upload-preview']",
            "[class*='file-card']",
            "[class*='file-chip']",
            "[class*='attached']",
            ".chat-input-area img:not([class*='avatar'])",
            ".input-area img:not([class*='avatar'])",
            "[class*='preview-item']",
            "[class*='upload-item']",
        ],
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# Visual Helper
# ═══════════════════════════════════════════════════════════════════════════════

async def _highlight(page: Page, element, color: str = "#ff0055") -> None:
    """Add a colored outline to an element in the browser for visibility."""
    try:
        await page.evaluate(
            """([el, col]) => {
                if (el) {
                    el.style.outline = '3px solid ' + col;
                    el.style.outlineOffset = '2px';
                    el.style.transition = 'all 0.2s';
                }
            }""",
            [element, color],
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Main Debugger Class
# ═══════════════════════════════════════════════════════════════════════════════

class QwenPromptDebugger:
    """Diagnostic tool for Qwen chat prompt page DOM/selectors."""

    DEFAULT_PROMPT = "Hello, reply with exactly: PONG"
    CONTINUE_PROMPT = "Now reply with exactly: PONG2"

    def __init__(
        self,
        account: str,
        email: str | None = None,
        password: str | None = None,
        headless: bool = False,
        clean_profile: bool = False,
        interactive: bool = False,
        skip_send: bool = False,
        skip_continue: bool = False,
        auto_login: bool = False,
        prompt: str | None = None,
        continue_prompt: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.account = account
        self.email = email
        self.password = password
        self.headless = headless
        self.clean_profile = clean_profile
        self.interactive = interactive
        self.skip_send = skip_send
        self.skip_continue = skip_continue
        self.auto_login_enabled = auto_login
        self.prompt = prompt or self.DEFAULT_PROMPT
        self.continue_prompt = continue_prompt or self.CONTINUE_PROMPT
        self.timeout = timeout

        self.playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.temp_profile_dir: Path | None = None

        self.debug_dir = DEBUG_DIR
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.ts_prefix = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Collect results for summary
        self._results: dict = {}

    def _resolve_credentials(self) -> tuple[str | None, str | None]:
        email = self.email
        password = self.password

        if not email or not password:
            auth_file = Path(QWEN_AUTH_CONFIG["auth_file"])
            if auth_file.exists():
                store = AuthStore(auth_file)
                creds = store.get(self.account)
                if creds:
                    email = email or creds.get("email")
                    password = password or creds.get("password")

        if not email:
            email = os.environ.get(QWEN_AUTH_CONFIG["env_email"])
        if not password:
            password = os.environ.get(QWEN_AUTH_CONFIG["env_password"])

        return email, password

    async def auto_login(self) -> bool:
        """Attempt automated login using resolved credentials."""
        email, password = self._resolve_credentials()
        if not email or not password:
            print("  [!] No credentials found for auto-login in authqwen.json or env.")
            return False

        masked = ("*" * len(password)) if password else ""
        print(f"[*] Attempting auto-login for '{self.account}' (email: {email}, pwd: {masked})...")
        login_url = QWEN_AUTH_CONFIG["login_url"]
        await self.page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2.0)

        # 1. Check two-step "Log in with a password" button
        for cand in [
            'button:has-text("Log in with a password")',
            'button:has-text("password")',
            ':has-text("Log in with a password")',
        ]:
            try:
                btn = await self.page.query_selector(cand)
                if btn and await btn.is_visible():
                    print("  [*] Switching to password login form...")
                    await btn.click()
                    await asyncio.sleep(1.5)
                    break
            except Exception:
                continue

        # 2. Email field
        email_el = None
        for sel in [
            'input[name="email"]',
            'input[placeholder*="Email" i]',
            'input[placeholder*="email" i]',
            'input[type="email"]',
            'input[type="text"]',
        ]:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3_000, state="visible")
                if el:
                    email_el = el
                    await el.click()
                    await el.fill(email)
                    print(f"  [+] Email filled via: {sel}")
                    break
            except Exception:
                continue

        # 3. Password field
        pwd_el = None
        for sel in [
            'input[name="password"]',
            'input[type="password"]',
            'input[placeholder*="Password" i]',
            'input[placeholder*="password" i]',
        ]:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3_000, state="visible")
                if el:
                    pwd_el = el
                    await el.click()
                    await el.fill(password)
                    print(f"  [+] Password filled via: {sel}")
                    break
            except Exception:
                continue

        if not email_el or not pwd_el:
            print("  [X] Failed to find email/password inputs on auth page.")
            return False

        # 4. Checkbox terms
        try:
            cb = await self.page.query_selector('input[type="checkbox"]:not(:checked)')
            if cb and await cb.is_visible():
                await cb.check()
                await asyncio.sleep(0.3)
        except Exception:
            pass

        # 5. Submit button
        submitted = False
        for sel in [
            'button[type="submit"].qwenchat-auth-pc-submit-button',
            '.qwenchat-auth-pc-submit button[type="submit"]',
            '.qwenchat-auth-pc-submit-button',
            'button[type="submit"]:has-text("Log in")',
            'button[type="submit"]',
            'button:has-text("Log in")',
            'button:has-text("Sign in")',
        ]:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    submitted = True
                    print(f"  [+] Login submit clicked via: {sel}")
                    break
            except Exception:
                continue

        if not submitted:
            await pwd_el.press("Enter")

        # 6. Wait for redirect
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            curr_url = self.page.url
            chat_input = await self.page.query_selector(
                'textarea[placeholder], div[contenteditable="true"], #chat-input'
            )
            if chat_input and await chat_input.is_visible():
                print(f"  [+] Auto-login succeeded! Landed at {curr_url}")
                await asyncio.sleep(2.0)
                return True
            if "/auth" not in curr_url and "/login" not in curr_url and "chat.qwen.ai" in curr_url:
                print(f"  [+] URL changed to {curr_url}. Login appears successful.")
                await asyncio.sleep(2.0)
                return True
            await asyncio.sleep(1.0)

        print("  [X] Auto-login timed out waiting for redirect to chat.")
        return False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        print("=" * 76)
        print("  QWEN PROMPT PAGE DOM & SELECTOR DEBUGGER")
        print("=" * 76)
        print(f"  Account      : {self.account}")
        print(f"  Headless     : {self.headless}")
        print(f"  Clean Profile: {self.clean_profile}")
        print(f"  Interactive  : {self.interactive}")
        print(f"  Skip Send    : {self.skip_send}")
        print(f"  Skip Continue: {self.skip_continue}")
        print(f"  Test Prompt  : {self.prompt[:60]}{'…' if len(self.prompt)>60 else ''}")
        print(f"  Debug Dir    : {self.debug_dir}")
        print("=" * 76)

        if self.clean_profile:
            self.temp_profile_dir = Path(tempfile.mkdtemp(prefix="qwen_debug_prompt_"))
            profile_dir = self.temp_profile_dir
            print(f"[*] Using clean temporary profile: {profile_dir}")
        else:
            profile_dir = PROFILES_DIR / "qwen" / self.account
            profile_dir.mkdir(parents=True, exist_ok=True)
            print(f"[*] Using persistent profile: {profile_dir}")

        self.playwright = await async_playwright().start()

        launch_args = list(QWEN_LAUNCH_ARGS)
        if "--disable-blink-features=AutomationControlled" not in launch_args:
            launch_args.append("--disable-blink-features=AutomationControlled")

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.headless,
            slow_mo=100 if not self.headless else 0,
            viewport=BROWSER_CONFIG["viewport"],
            user_agent=BROWSER_CONFIG["user_agent"],
            locale=BROWSER_CONFIG["locale"],
            timezone_id=BROWSER_CONFIG["timezone_id"],
            args=launch_args,
        )

        # Basic stealth
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def close(self) -> None:
        if self.interactive and not self.headless:
            print("\n[INTERACTIVE] Browser remains open for manual inspection.")
            print("Press [Enter] in this terminal to close the browser...")
            await asyncio.get_event_loop().run_in_executor(None, input)

        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

        if self.temp_profile_dir and self.temp_profile_dir.exists():
            try:
                shutil.rmtree(self.temp_profile_dir, ignore_errors=True)
            except Exception:
                pass

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _save_artifacts(self, step_name: str) -> None:
        if not self.page:
            return
        ss_path = self.debug_dir / f"qwen_prompt_{self.ts_prefix}_{step_name}.png"
        html_path = self.debug_dir / f"qwen_prompt_{self.ts_prefix}_{step_name}.html"
        try:
            await self.page.screenshot(path=str(ss_path), full_page=True)
            html = await self.page.content()
            html_path.write_text(html, encoding="utf-8")
            print(f"  [Artifact] Screenshot : {ss_path.name}")
            print(f"  [Artifact] HTML Dump  : {html_path.name}")
        except Exception as e:
            print(f"  [Artifact] Failed to save {step_name}: {e}")

    async def _pause(self, msg: str = "Press [Enter] to continue...") -> None:
        if self.interactive and not self.headless:
            print(f"\n[INTERACTIVE] {msg} (Open DevTools F12 in browser to inspect)")
            await asyncio.get_event_loop().run_in_executor(None, input)

    # ── STEP 1: Check Landing Page & Auth ─────────────────────────────────

    async def step1_check_landing(self) -> dict:
        print("\n--- [STEP 1] Check Landing Page & Authentication Status ---")
        base_url = QWEN_CONFIG["base_url"]
        print(f"[*] Navigating to {base_url} ...")
        await self.page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2.5)

        current_url = self.page.url
        title = await self.page.title()
        print(f"[*] Page loaded: URL = {current_url} | Title = {title}")

        # Check unauthenticated selectors
        any_unauth = False
        for sel in QWEN_AUTH_CONFIG["unauthenticated_selectors"]:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    any_unauth = True
                    print(f"  [!] Unauthenticated selector VISIBLE: {sel}")
                    break
            except Exception:
                continue

        # Check chat input exists
        chat_input = await self.page.query_selector(
            'textarea[placeholder], div[contenteditable="true"], #chat-input'
        )
        has_chat_input = chat_input and await chat_input.is_visible()

        if any_unauth:
            is_logged_in = False
            print("[-] Login/Sign Up button detected: User is NOT LOGGED IN.")
            print("    Run debug_login_qwen.py first, or login manually.")
        elif has_chat_input:
            is_logged_in = True
            print("[+] Chat input active & no login button: User IS LOGGED IN.")
        else:
            is_logged_in = False
            print("[?] No chat input and no login button: UNKNOWN state.")

        await self._save_artifacts("step1_landing")
        await self._pause("Landing page inspection complete.")

        result = {
            "current_url": current_url,
            "is_logged_in": is_logged_in,
            "has_chat_input": has_chat_input,
        }
        self._results["step1"] = result
        return result

    # ── STEP 2: Full DOM Scan of Chat Page ─────────────────────────────────

    async def step2_scan_chat_dom(self, label: str = "new_chat") -> dict:
        print(f"\n--- [STEP 2] Full DOM Scan of Chat Page ({label}) ---")

        # 2a. Scan all textarea / contenteditable elements
        input_scan = await self.page.evaluate("""
        () => {
            const results = [];
            const els = document.querySelectorAll('textarea, div[contenteditable="true"], input[type="text"]');
            els.forEach((el, idx) => {
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none';
                results.push({
                    index: idx,
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    id: el.id || '',
                    name: el.name || '',
                    placeholder: el.placeholder || '',
                    className: (el.className || '').toString().slice(0, 80),
                    contentEditable: el.contentEditable,
                    ariaLabel: el.getAttribute('aria-label') || '',
                    dataTestid: el.getAttribute('data-testid') || '',
                    visible: visible,
                    rows: el.rows || 0,
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                });
            });
            return results;
        }
        """)
        print(f"[*] Input elements (textarea/contenteditable) found: {len(input_scan)}")
        for inp in input_scan:
            vis = "VISIBLE" if inp["visible"] else "Hidden"
            print(
                f"    [{inp['index']}] <{inp['tag']}> id='{inp['id']}' placeholder='{inp['placeholder'][:30]}' "
                f"data-testid='{inp['dataTestid']}' class='{inp['className'][:40]}' "
                f"| {vis} ({inp['width']}x{inp['height']})"
            )

        # 2b. Scan all buttons
        button_scan = await self.page.evaluate("""
        () => {
            const results = [];
            document.querySelectorAll('button, [role="button"]').forEach((el, idx) => {
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none';
                const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
                results.push({
                    index: idx,
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    text: text,
                    id: el.id || '',
                    className: (el.className || '').toString().slice(0, 80),
                    ariaLabel: el.getAttribute('aria-label') || '',
                    dataTestid: el.getAttribute('data-testid') || '',
                    disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                    visible: visible,
                });
            });
            return results;
        }
        """)
        print(f"\n[*] Buttons found: {len(button_scan)}")
        for btn in button_scan:
            vis = "VISIBLE" if btn["visible"] else "Hidden"
            dis = "DISABLED" if btn["disabled"] else "Enabled"
            print(
                f"    [{btn['index']}] text='{btn['text']}' aria='{btn['ariaLabel'][:20]}' "
                f"testid='{btn['dataTestid']}' class='{btn['className'][:40]}' "
                f"| {vis} | {dis}"
            )

        # 2c. Scan think-mode related elements
        think_scan = await self.page.evaluate("""
        () => {
            const known = ['auto', 'thinking', 'fast'];
            const found = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            let node;
            while ((node = walker.nextNode())) {
                const text = (node.innerText || '').trim().toLowerCase();
                const cls = (node.className || '').toString().toLowerCase();
                if (
                    (known.includes(text) && node.offsetParent !== null) ||
                    (cls.includes('thinking') && node.offsetParent !== null) ||
                    (cls.includes('think-mode') && node.offsetParent !== null) ||
                    (cls.includes('qwen-select') && node.offsetParent !== null)
                ) {
                    const rect = node.getBoundingClientRect();
                    found.push({
                        tag: node.tagName,
                        className: (node.className || '').toString().slice(0, 80),
                        text: text.slice(0, 40),
                        id: node.id,
                        visible: rect.width > 0 && rect.height > 0,
                    });
                }
            }
            return found;
        }
        """)
        print(f"\n[*] Think-mode related elements: {len(think_scan)}")
        for el in think_scan:
            vis = "VISIBLE" if el["visible"] else "Hidden"
            print(f"    <{el['tag']}> text='{el['text']}' class='{el['className'][:50]}' | {vis}")

        # 2d. Scan response message containers currently in DOM
        response_scan = await self.page.evaluate("""
        () => {
            const selectors = [
                '.chat-message-container .chat-response-message',
                '.qwen-markdown-loose',
                '.qwen-markdown',
                '.chat-response-message',
                '[class*="response-message"]',
                '[class*="chat-response"]',
                '[class*="assistant-message"]',
                '[data-role="assistant"]',
                '[data-author="assistant"]',
            ];
            const counts = {};
            for (const sel of selectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    let visCount = 0;
                    els.forEach(e => {
                        if (e.offsetParent !== null) visCount++;
                    });
                    counts[sel] = { total: els.length, visible: visCount };
                } catch (e) {
                    counts[sel] = { total: 0, visible: 0, error: e.toString() };
                }
            }
            return counts;
        }
        """)
        print(f"\n[*] Response container element counts (current DOM):")
        for sel, info in response_scan.items():
            print(f"    {sel:<55} : total={info['total']}, visible={info['visible']}")

        # 2e. Scan toolbar / mode-select
        toolbar_scan = await self.page.evaluate("""
        () => {
            const selectors = [
                '.mode-select-open',
                '[class*="mode-select-open"]',
                'button[class*="mode-select"]',
                '[class*="mode-select-btn"]',
                '[class*="mode-select-trigger"]',
            ];
            const found = [];
            for (const sel of selectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    els.forEach(el => {
                        found.push({
                            selector: sel,
                            tag: el.tagName,
                            className: (el.className || '').toString().slice(0, 60),
                            visible: el.offsetParent !== null,
                            text: (el.innerText || '').trim().slice(0, 30),
                        });
                    });
                } catch(e) {}
            }
            return found;
        }
        """)
        print(f"\n[*] Toolbar / mode-select elements: {len(toolbar_scan)}")
        for el in toolbar_scan:
            vis = "VISIBLE" if el["visible"] else "Hidden"
            print(f"    {el['selector']:<40} <{el['tag']}> text='{el['text']}' | {vis}")

        await self._save_artifacts(f"step2_dom_scan_{label}")
        await self._pause(f"DOM scan ({label}) complete.")

        result = {
            "inputs": input_scan,
            "buttons": button_scan,
            "think_mode_elements": think_scan,
            "response_containers": response_scan,
            "toolbar_elements": toolbar_scan,
        }
        self._results[f"step2_{label}"] = result
        return result

    # ── STEP 3: Test Selector Registry vs Live DOM ────────────────────────

    async def step3_test_selectors(self, label: str = "new_chat") -> dict:
        print(f"\n--- [STEP 3] Test Selector Registry vs Live DOM ({label}) ---")

        results = {}
        for category, info in SELECTOR_REGISTRY.items():
            print(f"\n  [{category}] — {info['description']}")
            print(f"    Source: {info['source']}")
            cat_results = []
            for sel in info["selectors"]:
                try:
                    elements = await self.page.query_selector_all(sel)
                    vis_count = 0
                    for el in elements:
                        try:
                            if await el.is_visible():
                                vis_count += 1
                                await _highlight(self.page, el, "#00ff88")
                        except Exception:
                            pass
                    status = f"MATCH ({len(elements)} el, {vis_count} visible)" if elements else "NO MATCH"
                    icon = "✅" if vis_count > 0 else ("⚠️" if len(elements) > 0 else "❌")
                    print(f"    {icon} {sel:<60} : {status}")
                    cat_results.append({
                        "selector": sel,
                        "matched": len(elements),
                        "visible": vis_count,
                    })
                except Exception as e:
                    print(f"    ❌ {sel:<60} : ERROR ({e})")
                    cat_results.append({"selector": sel, "error": str(e)})
            results[category] = cat_results

        await self._save_artifacts(f"step3_selectors_{label}")
        await self._pause(f"Selector test ({label}) complete.")

        self._results[f"step3_{label}"] = results
        return results

    # ── STEP 4: Test Input Flow (NEW mode) ────────────────────────────────

    async def step4_test_new_mode(self) -> dict:
        print("\n--- [STEP 4] Test Input/Output Flow — NEW Mode ---")

        if self.skip_send:
            print("[*] --skip-send active. Skipping prompt submission.")
            result = {"skipped": True, "mode": "new"}
            self._results["step4_new"] = result
            return result

        # 4a. Navigate to new chat
        new_url = QWEN_CONFIG["new_chat_url"]
        print(f"[*] Navigating to new chat: {new_url}")
        await self.page.goto(new_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2.0)

        # 4b. Find input
        print("[*] Looking for chat input field...")
        input_el = None
        input_sel_used = None
        for sel in SELECTOR_REGISTRY["input_textarea"]["selectors"]:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3_000, state="visible")
                if el:
                    input_el = el
                    input_sel_used = sel
                    print(f"  [+] Input found: {sel}")
                    await _highlight(self.page, el, "#00ff88")
                    break
            except Exception:
                continue

        if not input_el:
            print("  [X] FAILED: Could not find chat input!")
            await self._save_artifacts("step4_new_no_input")
            result = {"success": False, "error": "input not found", "mode": "new"}
            self._results["step4_new"] = result
            return result

        # 4c. Count response elements BEFORE sending
        pre_count = await self.page.evaluate("""
        () => document.querySelectorAll(
            '.chat-message-container .chat-response-message, .qwen-markdown-loose, .qwen-markdown'
        ).length
        """)
        print(f"[*] Response element count BEFORE send: {pre_count}")

        # 4d. Fill prompt
        print(f"[*] Filling prompt: '{self.prompt[:50]}...'")
        await input_el.click()
        await input_el.type("~", delay=10)
        await input_el.fill(self.prompt)
        await asyncio.sleep(0.5)
        await self._save_artifacts("step4_new_filled")

        # 4e. Find and click send button
        print("[*] Looking for send button...")
        send_btn = None
        send_sel_used = None
        for sel in SELECTOR_REGISTRY["send_button"]["selectors"]:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    disabled = await el.get_attribute("disabled")
                    aria_dis = await el.get_attribute("aria-disabled")
                    if disabled is None and aria_dis != "true":
                        send_btn = el
                        send_sel_used = sel
                        print(f"  [+] Send button found (enabled): {sel}")
                        await _highlight(self.page, el, "#ffbb00")
                        break
                    else:
                        print(f"  [~] Send button found but DISABLED: {sel}")
            except Exception:
                continue

        if not send_btn:
            print("  [X] FAILED: Could not find enabled send button!")
            print("  [*] Attempting Enter key fallback...")
            await input_el.press("Enter")
        else:
            print("[*] Clicking send button...")
            try:
                await send_btn.click()
            except Exception as e:
                print(f"  [!] Click failed ({e}), trying Enter fallback...")
                await input_el.press("Enter")

        await asyncio.sleep(1.0)
        await self._save_artifacts("step4_new_sent")

        # 4f. Wait for response
        print("[*] Waiting for AI response...")
        response_text = ""
        response_ok = False
        deadline = time.monotonic() + self.timeout

        # Phase 1: wait for generation signal
        signal_deadline = time.monotonic() + 15.0
        generation_detected = False
        while time.monotonic() < signal_deadline:
            # Check stop button
            for sel in SELECTOR_REGISTRY["stop_button"]["selectors"]:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        generation_detected = True
                        print(f"  [+] Generation signal: stop button visible ({sel})")
                        break
                except Exception:
                    pass
            if generation_detected:
                break
            # Check streaming indicators
            for sel in SELECTOR_REGISTRY["streaming_indicators"]["selectors"]:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        generation_detected = True
                        print(f"  [+] Generation signal: streaming indicator visible ({sel})")
                        break
                except Exception:
                    pass
            if generation_detected:
                break
            # Check new response element
            post_count = await self.page.evaluate("""
            () => document.querySelectorAll(
                '.chat-message-container .chat-response-message, .qwen-markdown-loose, .qwen-markdown'
            ).length
            """)
            if post_count > pre_count:
                generation_detected = True
                print(f"  [+] Generation signal: response count {pre_count} → {post_count}")
                break
            await asyncio.sleep(0.5)

        if not generation_detected:
            print("  [!] WARNING: No generation signal detected after 15s")

        # Phase 2: wait for response to stabilize
        prev_text = ""
        stable_count = 0
        while time.monotonic() < deadline:
            # Check if still generating
            is_gen = await self.page.evaluate("""
            () => {
                const stopSels = [
                    'button[class*="stop"]', 'button[aria-label*="stop" i]',
                    'button[title*="stop" i]', '[class*="stop-btn"]', '[class*="abort"]',
                ];
                for (const s of stopSels) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null) return true;
                }
                const streamSels = [
                    '[class*="streaming"]', '[class*="typing"]', '[class*="loading-dots"]',
                    '[class*="thinking-indicator"]', '[class*="cursor-blink"]', '.result-streaming',
                ];
                for (const s of streamSels) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null) return true;
                }
                return false;
            }
            """)

            # Extract response
            current_text = await self.page.evaluate("""
            () => {
                const strategies = [
                    () => {
                        const containers = document.querySelectorAll(
                            '.chat-message-container .chat-response-message'
                        );
                        if (!containers.length) return null;
                        const last = containers[containers.length - 1];
                        const md = last.querySelector('.qwen-markdown-loose, .qwen-markdown');
                        const t = ((md || last).innerText || '').trim();
                        return t.length ? t : null;
                    },
                    () => {
                        const els = document.querySelectorAll('.qwen-markdown-loose, .qwen-markdown');
                        if (!els.length) return null;
                        const t = (els[els.length - 1].innerText || '').trim();
                        return t.length ? t : null;
                    },
                    () => {
                        const els = document.querySelectorAll('.chat-response-message');
                        if (!els.length) return null;
                        const t = (els[els.length - 1].innerText || '').trim();
                        return t.length ? t : null;
                    },
                    () => {
                        const sels = [
                            '[class*="response-message"]', '[class*="chat-response"]',
                            '[class*="assistant-message"]', '[data-role="assistant"]',
                            '[data-author="assistant"]',
                        ];
                        for (const s of sels) {
                            const els = document.querySelectorAll(s);
                            if (!els.length) continue;
                            const t = (els[els.length - 1].innerText || '').trim();
                            if (t.length > 3) return t;
                        }
                        return null;
                    },
                ];
                for (const fn of strategies) {
                    try { const r = fn(); if (r && r.length > 0) return r; } catch (e) {}
                }
                return '';
            }
            """)
            current_text = (current_text or "").strip()

            if current_text and not is_gen:
                if current_text == prev_text:
                    stable_count += 1
                    if stable_count >= 2:
                        response_text = current_text
                        response_ok = True
                        break
                else:
                    stable_count = 0
            else:
                stable_count = 0

            prev_text = current_text
            await asyncio.sleep(1.0)

        # Report
        if response_ok:
            print(f"  [+] Response received ({len(response_text)} chars):")
            print(f"      '{response_text[:200]}{'…' if len(response_text)>200 else ''}'")
        else:
            print(f"  [X] Response NOT received within {self.timeout}s timeout!")
            print(f"      Last extracted text ({len(prev_text)} chars): '{prev_text[:100]}'")

        # 4g. Post-response DOM check
        post_count = await self.page.evaluate("""
        () => document.querySelectorAll(
            '.chat-message-container .chat-response-message, .qwen-markdown-loose, .qwen-markdown'
        ).length
        """)
        print(f"[*] Response element count AFTER response: {post_count} (was {pre_count})")

        # Get conversation URL
        conv_url = self.page.url
        print(f"[*] Current URL (conversation): {conv_url}")

        await self._save_artifacts("step4_new_response")
        await self._pause("NEW mode test complete.")

        result = {
            "mode": "new",
            "success": response_ok,
            "input_selector": input_sel_used,
            "send_selector": send_sel_used,
            "generation_detected": generation_detected,
            "response_text_len": len(response_text),
            "response_preview": response_text[:200],
            "pre_count": pre_count,
            "post_count": post_count,
            "conversation_url": conv_url,
        }
        self._results["step4_new"] = result
        return result

    # ── STEP 5: Test CONTINUE Mode ────────────────────────────────────────

    async def step5_test_continue_mode(self) -> dict:
        print("\n--- [STEP 5] Test Input/Output Flow — CONTINUE Mode ---")

        if self.skip_send or self.skip_continue:
            reason = "--skip-send" if self.skip_send else "--skip-continue"
            print(f"[*] {reason} active. Skipping CONTINUE mode test.")
            result = {"skipped": True, "mode": "continue"}
            self._results["step5_continue"] = result
            return result

        step4 = self._results.get("step4_new", {})
        if not step4.get("success"):
            print("[!] NEW mode test did not succeed. Skipping CONTINUE to avoid confusion.")
            result = {"skipped": True, "mode": "continue", "reason": "new_mode_failed"}
            self._results["step5_continue"] = result
            return result

        print(f"[*] Staying on current conversation page: {self.page.url}")
        print(f"[*] Will send 2nd prompt: '{self.continue_prompt[:50]}...'")

        # 5a. Scan DOM differences vs NEW mode
        print("\n[*] Scanning DOM differences in CONTINUE mode...")
        await self.step2_scan_chat_dom(label="continue")
        await self.step3_test_selectors(label="continue")

        # 5b. Find input
        input_el = None
        input_sel_used = None
        for sel in SELECTOR_REGISTRY["input_textarea"]["selectors"]:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3_000, state="visible")
                if el:
                    input_el = el
                    input_sel_used = sel
                    print(f"  [+] Input found in CONTINUE: {sel}")
                    break
            except Exception:
                continue

        if not input_el:
            print("  [X] FAILED: Could not find chat input in CONTINUE mode!")
            await self._save_artifacts("step5_continue_no_input")
            result = {"success": False, "error": "input not found", "mode": "continue"}
            self._results["step5_continue"] = result
            return result

        # 5c. Count responses before
        pre_count = await self.page.evaluate("""
        () => document.querySelectorAll(
            '.chat-message-container .chat-response-message, .qwen-markdown-loose, .qwen-markdown'
        ).length
        """)
        print(f"[*] Response element count BEFORE 2nd send: {pre_count}")

        # 5d. Fill & send
        print(f"[*] Filling continue prompt: '{self.continue_prompt[:50]}...'")
        await input_el.click()
        await input_el.type("~", delay=10)
        await input_el.fill(self.continue_prompt)
        await asyncio.sleep(0.5)

        send_btn = None
        send_sel_used = None
        for sel in SELECTOR_REGISTRY["send_button"]["selectors"]:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    disabled = await el.get_attribute("disabled")
                    aria_dis = await el.get_attribute("aria-disabled")
                    if disabled is None and aria_dis != "true":
                        send_btn = el
                        send_sel_used = sel
                        break
            except Exception:
                continue

        if send_btn:
            print(f"  [+] Send button found: {send_sel_used}")
            try:
                await send_btn.click()
            except Exception:
                await input_el.press("Enter")
        else:
            print("  [!] No send button, using Enter fallback")
            await input_el.press("Enter")

        await asyncio.sleep(1.0)
        await self._save_artifacts("step5_continue_sent")

        # 5e. Wait for response
        print("[*] Waiting for AI response (CONTINUE)...")
        response_text = ""
        response_ok = False
        generation_detected = False
        deadline = time.monotonic() + self.timeout
        prev_text = ""
        stable_count = 0

        # Wait for generation signal
        signal_deadline = time.monotonic() + 15.0
        while time.monotonic() < signal_deadline:
            post_count = await self.page.evaluate("""
            () => document.querySelectorAll(
                '.chat-message-container .chat-response-message, .qwen-markdown-loose, .qwen-markdown'
            ).length
            """)
            if post_count > pre_count:
                generation_detected = True
                print(f"  [+] Generation signal: response count {pre_count} → {post_count}")
                break
            is_gen = await self.page.evaluate("""
            () => {
                const sels = ['button[class*="stop"]','button[aria-label*="stop" i]',
                    '[class*="streaming"]','[class*="typing"]'];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null) return true;
                }
                return false;
            }
            """)
            if is_gen:
                generation_detected = True
                print("  [+] Generation signal: streaming/stop detected")
                break
            await asyncio.sleep(0.5)

        if not generation_detected:
            print("  [!] WARNING: No generation signal in CONTINUE mode after 15s")

        # Wait for stable response
        while time.monotonic() < deadline:
            is_gen = await self.page.evaluate("""
            () => {
                const sels = ['button[class*="stop"]','button[aria-label*="stop" i]',
                    '[class*="streaming"]','[class*="typing"]','[class*="loading-dots"]'];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null) return true;
                }
                return false;
            }
            """)
            current_text = await self.page.evaluate("""
            () => {
                const els = document.querySelectorAll(
                    '.chat-message-container .chat-response-message, .qwen-markdown-loose, .qwen-markdown'
                );
                if (!els.length) return '';
                return (els[els.length - 1].innerText || '').trim();
            }
            """)
            current_text = (current_text or "").strip()

            if current_text and not is_gen:
                if current_text == prev_text:
                    stable_count += 1
                    if stable_count >= 2:
                        response_text = current_text
                        response_ok = True
                        break
                else:
                    stable_count = 0
            else:
                stable_count = 0
            prev_text = current_text
            await asyncio.sleep(1.0)

        if response_ok:
            print(f"  [+] CONTINUE response received ({len(response_text)} chars):")
            print(f"      '{response_text[:200]}{'…' if len(response_text)>200 else ''}'")
        else:
            print(f"  [X] CONTINUE response NOT received within {self.timeout}s!")

        post_count_final = await self.page.evaluate("""
        () => document.querySelectorAll(
            '.chat-message-container .chat-response-message, .qwen-markdown-loose, .qwen-markdown'
        ).length
        """)
        print(f"[*] Response count AFTER continue: {post_count_final} (was {pre_count})")

        await self._save_artifacts("step5_continue_response")
        await self._pause("CONTINUE mode test complete.")

        result = {
            "mode": "continue",
            "success": response_ok,
            "input_selector": input_sel_used,
            "send_selector": send_sel_used,
            "generation_detected": generation_detected,
            "response_text_len": len(response_text),
            "response_preview": response_text[:200],
            "pre_count": pre_count,
            "post_count": post_count_final,
        }
        self._results["step5_continue"] = result
        return result

    # ── STEP 6: Summary ───────────────────────────────────────────────────

    def save_summary(self) -> tuple[Path, Path]:
        summary_dir = self.debug_dir / "summary_prompt"
        summary_dir.mkdir(parents=True, exist_ok=True)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── Build selector evaluation table ──
        selector_eval = {}
        for label_key in ["step3_new_chat", "step3_continue"]:
            data = self._results.get(label_key, {})
            for category, results in data.items():
                if category not in selector_eval:
                    selector_eval[category] = {"new": [], "continue": []}
                mode = "new" if "new" in label_key else "continue"
                selector_eval[category][mode] = results

        # ── JSON ──
        summary_dict = {
            "metadata": {
                "generated_at": now_str,
                "account": self.account,
                "headless": self.headless,
                "test_prompt": self.prompt,
                "continue_prompt": self.continue_prompt,
            },
            "results": self._results,
            "selector_evaluation": selector_eval,
        }

        json_latest = summary_dir / "summary.json"
        json_archive = summary_dir / f"summary_{self.ts_prefix}.json"
        json_content = json.dumps(summary_dict, indent=2, ensure_ascii=False, default=str)
        json_latest.write_text(json_content, encoding="utf-8")
        json_archive.write_text(json_content, encoding="utf-8")

        # ── Markdown ──
        md = [
            "# Qwen Prompt Page DOM & Selector Diagnostic Summary",
            "",
            f"- **Generated**: {now_str}",
            f"- **Account**: `{self.account}`",
            f"- **Headless**: `{self.headless}`",
            f"- **Test Prompt**: `{self.prompt[:60]}`",
            "",
            "---",
            "",
            "## 1. Auth & Landing",
            "",
        ]

        s1 = self._results.get("step1", {})
        md.append(f"- Logged in: **{'YES' if s1.get('is_logged_in') else 'NO'}**")
        md.append(f"- Chat input found: **{'YES' if s1.get('has_chat_input') else 'NO'}**")
        md.append("")

        # ── Selector table ──
        md += [
            "## 2. Selector Evaluation (NEW mode)",
            "",
            "| Category | Selector | Matched | Visible | Status |",
            "| :--- | :--- | :---: | :---: | :--- |",
        ]
        for category, modes in selector_eval.items():
            for r in modes.get("new", []):
                matched = r.get("matched", 0)
                visible = r.get("visible", 0)
                status = "✅ OK" if visible > 0 else ("⚠️ Hidden" if matched > 0 else "❌ NOT FOUND")
                sel = r.get("selector", "?")
                md.append(f"| **{category}** | `{sel}` | {matched} | {visible} | {status} |")
        md.append("")

        # ── CONTINUE mode selectors (if available) ──
        has_cont = any(modes.get("continue") for modes in selector_eval.values())
        if has_cont:
            md += [
                "## 3. Selector Evaluation (CONTINUE mode)",
                "",
                "| Category | Selector | Matched | Visible | Status |",
                "| :--- | :--- | :---: | :---: | :--- |",
            ]
            for category, modes in selector_eval.items():
                for r in modes.get("continue", []):
                    matched = r.get("matched", 0)
                    visible = r.get("visible", 0)
                    status = "✅ OK" if visible > 0 else ("⚠️ Hidden" if matched > 0 else "❌ NOT FOUND")
                    sel = r.get("selector", "?")
                    md.append(f"| **{category}** | `{sel}` | {matched} | {visible} | {status} |")
            md.append("")

        # ── I/O test results ──
        md += ["## 4. Input/Output Test Results", ""]
        for key, label in [("step4_new", "NEW Mode"), ("step5_continue", "CONTINUE Mode")]:
            data = self._results.get(key, {})
            if data.get("skipped"):
                md.append(f"### {label}: SKIPPED")
            else:
                success = data.get("success", False)
                md.append(f"### {label}: **{'PASS' if success else 'FAIL'}**")
                md.append(f"- Input selector used: `{data.get('input_selector', 'N/A')}`")
                md.append(f"- Send selector used: `{data.get('send_selector', 'N/A')}`")
                md.append(f"- Generation detected: {data.get('generation_detected', 'N/A')}")
                md.append(f"- Response length: {data.get('response_text_len', 0)} chars")
                md.append(f"- Response count: {data.get('pre_count', '?')} → {data.get('post_count', '?')}")
                preview = data.get("response_preview", "")
                if preview:
                    md.append(f"- Preview: `{preview[:100]}`")
            md.append("")

        # ── Selectors needing update ──
        md += ["---", "", "## 5. Selectors Needing Update", ""]
        needs_update = []
        for category, modes in selector_eval.items():
            new_results = modes.get("new", [])
            if new_results and not any(r.get("visible", 0) > 0 for r in new_results):
                needs_update.append(category)
        if needs_update:
            for cat in needs_update:
                info = SELECTOR_REGISTRY.get(cat, {})
                md.append(f"- **{cat}**: {info.get('description', '')} — NO visible match in NEW mode")
                md.append(f"  - Source: `{info.get('source', '')}`")
                for sel in info.get("selectors", []):
                    md.append(f"  - `{sel}` ❌")
                md.append("")
        else:
            md.append("All selector categories had at least one visible match. ✅")
            md.append("")

        md_latest = summary_dir / "summary.md"
        md_archive = summary_dir / f"summary_{self.ts_prefix}.md"
        md_content = "\n".join(md)
        md_latest.write_text(md_content, encoding="utf-8")
        md_archive.write_text(md_content, encoding="utf-8")

        return md_latest, json_latest

    def print_summary(self) -> None:
        print("\n" + "=" * 76)
        print("  DIAGNOSTIC SUMMARY")
        print("=" * 76)

        # Auth
        s1 = self._results.get("step1", {})
        print(f"  Auth Status    : {'LOGGED IN' if s1.get('is_logged_in') else 'NOT LOGGED IN'}")

        # Selector categories
        for label_key, mode_name in [("step3_new_chat", "NEW"), ("step3_continue", "CONTINUE")]:
            data = self._results.get(label_key, {})
            if not data:
                continue
            print(f"\n  Selector Results ({mode_name} mode):")
            for category, results in data.items():
                any_visible = any(r.get("visible", 0) > 0 for r in results)
                mark = "✅ OK" if any_visible else "❌ NEEDS UPDATE"
                print(f"    {category:<30} : {mark}")

        # I/O tests
        for key, name in [("step4_new", "NEW I/O"), ("step5_continue", "CONTINUE I/O")]:
            data = self._results.get(key, {})
            if data.get("skipped"):
                print(f"\n  {name} Test : SKIPPED")
            else:
                ok = data.get("success", False)
                print(f"\n  {name} Test : {'PASS ✅' if ok else 'FAIL ❌'}")
                if ok:
                    print(f"    Response: {data.get('response_text_len', 0)} chars")

        # Save summary
        try:
            md_path, json_path = self.save_summary()
            print(f"\n  [SUMMARY SAVED]:")
            print(f"    Markdown : {md_path}")
            print(f"    JSON     : {json_path}")
        except Exception as e:
            print(f"\n  [!] Failed to save summary: {e}")

        print(f"  Artifacts (screenshots & HTML) in: {self.debug_dir}")
        print("=" * 76)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

async def main_async():
    parser = argparse.ArgumentParser(
        description="Diagnostic tool for Qwen chat prompt page DOM/selectors (input & output)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--account", default="account1",
        help="Account name from cookies/authqwen.json (default: account1)",
    )
    parser.add_argument(
        "--email", default=None,
        help="Override login email for auto-login",
    )
    parser.add_argument(
        "--password", default=None,
        help="Override login password for auto-login",
    )
    parser.add_argument(
        "--auto-login", action="store_true", default=False,
        help="Automatically log in if session is not active",
    )
    parser.add_argument(
        "--headless", action="store_true", default=False,
        help="Run browser in headless mode (default: visible)",
    )
    parser.add_argument(
        "--clean-profile", action="store_true", default=False,
        help="Use a temporary clean profile (first-time login needed)",
    )
    parser.add_argument(
        "--interactive", action="store_true", default=False,
        help="Pause at each step for manual inspection (DevTools F12)",
    )
    parser.add_argument(
        "--skip-send", action="store_true", default=False,
        help="Only scan DOM without sending any prompt",
    )
    parser.add_argument(
        "--skip-continue", action="store_true", default=False,
        help="Skip CONTINUE mode test (only test NEW mode)",
    )
    parser.add_argument(
        "--prompt", default=None,
        help="Custom prompt to send for NEW mode test (default: 'Hello, reply with exactly: PONG')",
    )
    parser.add_argument(
        "--continue-prompt", default=None,
        help="Custom prompt for CONTINUE mode test (default: 'Now reply with exactly: PONG2')",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0,
        help="Timeout (seconds) for waiting for AI response (default: 60)",
    )

    args = parser.parse_args()

    debugger = QwenPromptDebugger(
        account=args.account,
        email=args.email,
        password=args.password,
        headless=args.headless,
        clean_profile=args.clean_profile,
        interactive=args.interactive,
        skip_send=args.skip_send,
        skip_continue=args.skip_continue,
        auto_login=args.auto_login,
        prompt=args.prompt,
        continue_prompt=args.continue_prompt,
        timeout=args.timeout,
    )

    try:
        await debugger.start()

        # Step 1: Check auth
        landing = await debugger.step1_check_landing()
        if not landing["is_logged_in"]:
            if args.auto_login:
                print("\n[*] Sesi belum login. Mencoba auto-login (--auto-login aktif)...")
                login_ok = await debugger.auto_login()
                if not login_ok:
                    print("\n[X] Auto-login gagal.")
                    debugger.print_summary()
                    return
                # Re-check landing
                landing = await debugger.step1_check_landing()
                if not landing["is_logged_in"]:
                    print("\n[X] Sesi masih belum login setelah auto-login.")
                    debugger.print_summary()
                    return
            else:
                print("\n[X] User is NOT logged in. Cannot test prompt page.")
                print("    Tips: jalankan dengan flag --auto-login untuk login otomatis:")
                print(f"      python debug_prompt_qwen.py --account {args.account} --auto-login")
                print("    Atau jalankan debug_login_qwen.py terlebih dahulu:")
                print(f"      python debug_login_qwen.py --account {args.account}")
                debugger.print_summary()
                return

        # Step 2: Full DOM scan (NEW chat page)
        await debugger.step2_scan_chat_dom(label="new_chat")

        # Step 3: Test selectors (NEW chat page)
        await debugger.step3_test_selectors(label="new_chat")

        # Step 4: Test NEW mode (send prompt, get response)
        await debugger.step4_test_new_mode()

        # Step 5: Test CONTINUE mode (send 2nd prompt in same conversation)
        await debugger.step5_test_continue_mode()

        # Print summary
        debugger.print_summary()

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user (Ctrl+C).")
    except Exception as e:
        logger.error("Error during debugging: %s", e, exc_info=True)
    finally:
        await debugger.close()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
