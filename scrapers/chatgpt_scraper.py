"""
scrapers/chatgpt_scraper.py — ChatGPTScraper(BaseAIChatScraper).

Implements the ChatGPT-specific login flow (per owner screenshots) and chat
flow (per design_chatgpt_backend.md §5–§8). See implementation.md Tahap C
for the step-by-step spec this file follows.

Login flow (headless=true is a HARD constraint — see config/chatgpt.py):
  1. goto https://chatgpt.com/
  2. "Log in" button absent → session already valid.
  3. "Log in" button present:
       a. click "Log in" — may open a POPUP window OR redirect same-tab.
          Both are handled via a `context.on("page")` listener installed
          BEFORE the click.
       b. email page → fill email → click "Continue"
       c. "Check your inbox" page → click "Continue with password"
          (THE MAIN PATH — never fills an email verification code)
       d. password page → fill password → click "Continue"
       e. wait for redirect back to the chat UI + "Log in" button gone.

Chat flow:
  NEW:      goto new_chat_url → fill prompt → send → wait_for_response
  CONTINUE: goto saved conversation_url (from SessionStore, passed via the
            `continue_url` kwarg) → fill prompt → send → wait_for_response

Response extraction:
  MAIN:     last `[data-message-author-role="assistant"]` node → inner_text()
  FALLBACK: innerText of <main>, anchored on the LAST occurrence of the
            prompt, cleaned per the openai.js reference (strip "You said:" /
            "ChatGPT said:" labels, disclaimer lines, UI button labels,
            de-duplicate consecutive lines).
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from config import CHATGPT_AUTH_CONFIG, CHATGPT_CONFIG
from scrapers.base_chatgpt import BaseAIChatScraper, _count_tokens

SEL = CHATGPT_CONFIG["selectors"]


class ChatGPTScraper(BaseAIChatScraper):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Set once a NEW chat has actually been opened / a CONTINUE goto has
        # landed on a conversation — mirrors DeepSeek's _conversation_started.
        self._conversation_started: bool = False

    # ── Small selector-list helpers (pola `_find_first` existing) ──── #

    async def _find_first(self, page: Page, selectors: list[str], timeout_ms: int = 4000):
        for sel in selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _click_first(self, page: Page, selectors: list[str], timeout_ms: int = 4000) -> bool:
        el = await self._find_first(page, selectors, timeout_ms)
        if el:
            try:
                await el.click()
                return True
            except Exception:
                return False
        return False

    async def _has_visible(self, page: Page, selectors: list[str], timeout_ms: int = 500) -> bool:
        for sel in selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
                if el:
                    return True
            except Exception:
                continue
        return False

    # ── C1. ensure_authenticated() — idempotent (pola Qwen) ─────────── #

    async def ensure_authenticated(self) -> bool:
        if self._page is None:
            await self.launch_browser(account=self.account)
        assert self._page is not None

        try:
            current_url = self._page.url or ""
            if "chatgpt.com" not in current_url:
                await self._page.goto(
                    CHATGPT_CONFIG["base_url"], wait_until="domcontentloaded", timeout=30_000,
                )
                await asyncio.sleep(1.5)  # beri waktu SPA render tombol Login
        except Exception as nav_err:
            self.logger.warning("ensure_authenticated: gagal navigasi: %s", nav_err)

        if await self._is_logged_in():
            self.logger.info(
                "ensure_authenticated: session account '%s' masih valid ✅", self.account,
            )
            self._authenticated = True
            return True

        self.logger.info(
            "ensure_authenticated: tombol 'Log in' terdeteksi untuk account '%s' → login otomatis",
            self.account,
        )
        ok = await self.login()
        self._authenticated = ok
        return ok

    # ── C2. login() — flow sesuai screenshot owner (headless=true) ──── #

    async def login(self, email: str | None = None, password: str | None = None) -> bool:
        if self._page is None:
            await self.launch_browser(account=self.account)
        assert self._page is not None
        assert self._context is not None

        email, password = self._resolve_credentials(email, password)
        if not email or not password:
            self.logger.error(
                "No credentials for account '%s'. Add to %s or set %s/%s",
                self.account, CHATGPT_AUTH_CONFIG["auth_file"],
                CHATGPT_AUTH_CONFIG["env_email"], CHATGPT_AUTH_CONFIG["env_password"],
            )
            return False

        # 0. Pastikan di halaman awal & tombol Log in terlihat.
        await self._page.goto(CHATGPT_CONFIG["base_url"], wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(1.0)

        # 1. Pasang listener POPUP sebelum klik "Log in".
        popup_page: Page | None = None

        def _on_page(p: Page) -> None:
            nonlocal popup_page
            popup_page = p

        self._context.on("page", _on_page)
        try:
            clicked = await self._click_first(self._page, SEL["login_button"], timeout_ms=8000)
            if not clicked:
                self.logger.error("Tombol 'Log in' tidak ditemukan")
                await self.take_debug_screenshot("login_button_not_found")
                return False

            await asyncio.sleep(1.5)  # beri waktu popup/redirect terbentuk

            auth = popup_page or self._page  # ← popup ATAU same-tab
            try:
                await auth.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass

            # 2. Halaman email → isi → Continue.
            if not await self._fill_email_page(auth, email):
                return False

            # 3. Halaman "Check your inbox" → KLIK "Continue with password".
            if not await self._click_continue_with_password(auth):
                return False

            # 4. Halaman password → isi → Continue.
            if not await self._fill_password_page(auth, password):
                return False

            # 5. Tunggu hasil.
            return await self._wait_for_login_result(auth)
        finally:
            try:
                self._context.remove_listener("page", _on_page)
            except Exception:
                pass

    async def _fill_email_page(self, page: Page, email: str) -> bool:
        login_sel = SEL["login"]
        email_el = await self._find_first(page, login_sel["email_input"], timeout_ms=8000)
        if not email_el:
            if await self._check_captcha(page):
                return False
            self.logger.error("Input email tidak ditemukan di halaman login")
            await self.take_debug_screenshot("login_email_page")
            return False
        try:
            await email_el.click()
            await email_el.fill(email)
            await asyncio.sleep(0.4)
        except Exception as exc:
            self.logger.error("Gagal mengisi email: %s", exc)
            await self.take_debug_screenshot("login_email_page")
            return False

        clicked = await self._click_continue_button(page, timeout_ms=5000)
        if not clicked:
            try:
                await email_el.press("Enter")
            except Exception:
                self.logger.error("Tombol Continue (email) tidak ditemukan")
                await self.take_debug_screenshot("login_email_page")
                return False
        await asyncio.sleep(1.2)
        return True

    async def _click_continue_with_password(self, page: Page) -> bool:
        login_sel = SEL["login"]
        el = await self._find_first(page, login_sel["continue_with_password"], timeout_ms=8000)
        if el:
            try:
                await el.click()
                await asyncio.sleep(1.0)
                return True
            except Exception as exc:
                self.logger.error("Gagal klik 'Continue with password': %s", exc)
                await self.take_debug_screenshot("login_continue_with_password")
                return False

        # Tidak ditemukan: cek apakah sudah langsung di halaman password
        # (beberapa akun tidak lewat halaman "Check your inbox").
        if await self._has_visible(page, login_sel["password_input"], timeout_ms=2000):
            self.logger.info("Halaman 'Check your inbox' dilewati — langsung ke halaman password")
            return True

        if await self._check_captcha(page):
            return False

        self.logger.error(
            "Tombol 'Continue with password' tidak ditemukan dan bukan halaman password. "
            "Kemungkinan verifikasi email dipaksa (tanpa opsi password)."
        )
        await self.take_debug_screenshot("login_no_continue_with_password")
        return False

    async def _fill_password_page(self, page: Page, password: str) -> bool:
        login_sel = SEL["login"]
        pwd_el = await self._find_first(page, login_sel["password_input"], timeout_ms=8000)
        if not pwd_el:
            if await self._check_captcha(page):
                return False
            self.logger.error("Input password tidak ditemukan")
            await self.take_debug_screenshot("login_password_page")
            return False
        try:
            await pwd_el.click()
            await pwd_el.fill(password)
            await asyncio.sleep(0.4)
        except Exception as exc:
            self.logger.error("Gagal mengisi password: %s", exc)
            await self.take_debug_screenshot("login_password_page")
            return False

        clicked = await self._click_continue_button(page, timeout_ms=5000)
        if not clicked:
            try:
                await pwd_el.press("Enter")
            except Exception:
                self.logger.error("Tombol Continue (password) tidak ditemukan")
                await self.take_debug_screenshot("login_password_page")
                return False
        return True

    async def _click_continue_button(self, page: Page, timeout_ms: int = 5000) -> bool:
        """Click the primary "Continue" button — never an alternate/secondary
        option like "Continue with phone number" / "Continue with Google".

        BUG FIX: `button:has-text("Continue")` matches by SUBSTRING in
        Playwright, so it also matches those secondary buttons. Even with
        the `:text-is("Continue")` selector fix in config/chatgpt.py, this
        method adds a runtime text check as a second line of defense: it
        verifies the actually-resolved element's own text is exactly
        "Continue" (case-insensitive, trimmed) before clicking, and skips to
        the next candidate selector otherwise.
        """
        login_sel = SEL["login"]
        for sel in login_sel["continue_button"]:
            try:
                el = await page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
            except Exception:
                continue
            if not el:
                continue
            try:
                text = ((await el.inner_text()) or "").strip().lower()
            except Exception:
                continue
            if text != "continue":
                self.logger.warning(
                    "Skipping continue_button candidate %r — resolved text is %r, "
                    "not exactly 'Continue' (likely a secondary option like "
                    "'Continue with phone number')", sel, text,
                )
                continue
            try:
                await el.click()
                return True
            except Exception:
                continue
        return False

    async def _check_captcha(self, page: Page) -> bool:
        """Jika captcha/Turnstile terdeteksi → fail loud + screenshot. Returns
        True if a captcha was detected (caller should abort)."""
        if await self._has_visible(page, SEL["captcha"], timeout_ms=500):
            self.logger.error(
                "Captcha terdeteksi saat login. Jalankan sekali dengan --no-headless; "
                "profile akan mengingat session."
            )
            await self.take_debug_screenshot("login_captcha")
            return True
        return False

    # ── C3. _wait_for_login_result — polling (pola DeepSeek) ────────── #

    async def _wait_for_login_result(self, auth_page: Page) -> bool:
        deadline = time.monotonic() + CHATGPT_AUTH_CONFIG["login_wait"]
        login_sel = SEL["login"]

        while time.monotonic() < deadline:
            # 1. Captcha/Turnstile.
            if await self._check_captcha(auth_page):
                return False

            # 2. Error inline (password salah, dll.)
            for sel in login_sel["error_message"]:
                try:
                    el = await auth_page.query_selector(sel)
                    if el and await el.is_visible():
                        txt = (await el.inner_text()).strip()
                        if txt:
                            self.logger.error("Error login ChatGPT: %s", txt)
                            await self.take_debug_screenshot("login_error")
                            return False
                except Exception:
                    pass

            # 3. Sukses: URL tidak lagi memuat pola auth DAN "Log in" hilang.
            url = auth_page.url or ""
            still_on_auth = any(
                pat in url.lower() for pat in CHATGPT_AUTH_CONFIG["auth_url_patterns"]
            )
            if not still_on_auth:
                # Popup case: tutup popup dan pastikan tab utama ter-refresh.
                if auth_page is not self._page:
                    try:
                        await auth_page.close()
                    except Exception:
                        pass
                    try:
                        await self._page.goto(
                            CHATGPT_CONFIG["base_url"], wait_until="domcontentloaded", timeout=15_000,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
                else:
                    try:
                        await self._page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except Exception:
                        pass

                if await self._is_logged_in():
                    await asyncio.sleep(CHATGPT_AUTH_CONFIG["post_login_settle"])
                    self.logger.info(
                        "Login ChatGPT berhasil — session disimpan di profile '%s'", self.account,
                    )
                    self._authenticated = True
                    profile_dir = self._profile_dir_for(self.account)
                    sentinel = profile_dir / "cookies_seeded"
                    if not sentinel.exists():
                        try:
                            sentinel.write_text("1", encoding="utf-8")
                        except Exception:
                            pass
                    return True

            await asyncio.sleep(1.0)

        self.logger.error(
            "Login ChatGPT timeout setelah %ss", CHATGPT_AUTH_CONFIG["login_wait"],
        )
        await self.take_debug_screenshot("login_timeout")
        return False

    # ── C4. send_prompt(prompt, mode, attachments) — flow chat ──────── #

    async def send_prompt(self, prompt: str, mode: str = "new", **kwargs) -> str:
        assert self._page is not None
        attachments = kwargs.get("attachments")
        continue_url = kwargs.get("continue_url")

        if mode == "continue":
            if not continue_url:
                raise ValueError(
                    "mode='continue' requires a conversation_url (session not found "
                    "or expired — caller should create a new session instead)."
                )
            self.logger.info("CONTINUE: navigating to %s", continue_url)
            await self._page.goto(continue_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.0)
            self._conversation_started = True
        else:
            self.logger.info("NEW: opening a fresh chat")
            await self._page.goto(
                CHATGPT_CONFIG["new_chat_url"], wait_until="domcontentloaded", timeout=30_000,
            )
            await asyncio.sleep(0.8)
            self._conversation_started = False

        textarea = await self._find_first(self._page, SEL["prompt_textarea"], timeout_ms=15_000)
        if not textarea:
            await self.take_debug_screenshot("prompt_textarea_not_found")
            raise RuntimeError("Prompt textarea not found on ChatGPT page")

        if attachments:
            await self._upload_attachments(attachments)

        try:
            await textarea.click()
            await textarea.fill(prompt)
        except Exception:
            # Fallback: type per-character.
            try:
                await textarea.click()
                await textarea.type(prompt, delay=10)
            except Exception as exc:
                await self.take_debug_screenshot("fill_prompt_failed")
                raise RuntimeError(f"Failed to fill prompt: {exc}") from exc

        await asyncio.sleep(CHATGPT_CONFIG["timeouts"]["between_actions"] / 1000)

        sent = await self._click_first(self._page, SEL["send_button"], timeout_ms=4000)
        if not sent:
            try:
                await textarea.press("Enter")
            except Exception as exc:
                await self.take_debug_screenshot("send_prompt_failed")
                raise RuntimeError(f"Failed to send prompt: {exc}") from exc

        await asyncio.sleep(0.5)
        response = await self.wait_for_response()
        return response

    # ── C5. Response extraction — DOM utama + fallback innerText ───── #

    async def _extract_response(self, prompt: str) -> str:
        assert self._page is not None
        for sel in SEL["assistant_message"]:
            try:
                nodes = await self._page.query_selector_all(sel)
                if nodes:
                    text = ((await nodes[-1].inner_text()) or "").strip()
                    if text:
                        return text
            except Exception:
                continue

        # FALLBACK: innerText prompt-anchored (port openai.js).
        main = None
        for sel in SEL["main_area"]:
            try:
                main = await self._page.query_selector(sel)
                if main:
                    break
            except Exception:
                continue
        if not main:
            return ""
        try:
            full = await main.inner_text()
        except Exception:
            return ""
        return self._clean_inner_text(full, prompt)

    _UI_LINE_PATTERNS = [
        r"^copy$", r"^share$", r"^regenerate$", r"^edit$", r"^retry$",
        r"^good response$", r"^bad response$", r"^read aloud$",
        r"^chatgpt can make mistakes.*", r"^chatgpt said:?$", r"^you said:?$",
    ]
    _UI_LINE_RE = re.compile("|".join(_UI_LINE_PATTERNS), re.IGNORECASE)

    def _clean_inner_text(self, full_text: str, prompt: str) -> str:
        """Port 1:1 fase cleaning openai.js: anchor pada occurrence TERAKHIR
        dari prompt, potong sampai sebelum blok berikutnya, buang label
        "You said:"/"ChatGPT said:", filter baris UI, dedupe baris berturutan.
        """
        if not full_text:
            return ""

        idx = full_text.rfind(prompt.strip()) if prompt else -1
        segment = full_text[idx + len(prompt):] if idx != -1 else full_text

        # Cut at the next "You said:" occurrence (start of a new turn), if any.
        next_turn = re.search(r"you said:?", segment, re.IGNORECASE)
        if next_turn and next_turn.start() > 0:
            segment = segment[: next_turn.start()]

        lines = [ln.strip() for ln in segment.splitlines()]
        cleaned: list[str] = []
        prev = None
        for ln in lines:
            if not ln:
                continue
            if self._UI_LINE_RE.match(ln):
                continue
            if ln == prev:
                continue
            cleaned.append(ln)
            prev = ln

        return "\n".join(cleaned).strip()

    # ── C6. _upload_attachments(attachments) ─────────────────────────── #

    async def _upload_attachments(self, attachments: list[dict]) -> None:
        assert self._page is not None
        from scrapers.utils import safe_filename  # noqa: F401 (kept for parity/logging)
        import base64
        import mimetypes
        import tempfile
        import os

        for att in attachments:
            filename = att.get("filename") or "attachment"
            b64_data = att.get("data") or ""
            mime_type = att.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"

            raw = b64_data
            if raw.startswith("data:"):
                _, _, raw = raw.partition(",")
            raw = raw.strip()
            missing = len(raw) % 4
            if missing:
                raw += "=" * (4 - missing)

            try:
                data = base64.b64decode(raw)
            except Exception as exc:
                raise RuntimeError(f"Invalid base64 for attachment '{filename}': {exc}") from exc

            suffix = Path(filename).suffix or mimetypes.guess_extension(mime_type) or ".bin"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="chatgpt_attach_")
            os.close(fd)
            Path(tmp_path).write_bytes(data)

            try:
                file_input = await self._find_first(self._page, SEL["file_input"], timeout_ms=5000)
                if not file_input:
                    raise RuntimeError("File input tidak ditemukan pada halaman ChatGPT")
                await file_input.set_input_files(tmp_path)

                ok = await self._has_visible(
                    self._page, SEL["attachment_preview"],
                    timeout_ms=CHATGPT_CONFIG["timeouts"]["attachment_preview"],
                )
                if not ok:
                    await self.take_debug_screenshot(f"attachment_preview_timeout_{filename}")
                    raise RuntimeError(
                        f"Attachment '{filename}' preview tidak muncul dalam batas waktu"
                    )
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    # ── C7. scrape() — orkestrasi + hasil ─────────────────────────────── #

    async def scrape(
        self,
        prompt: str,
        mode: str = "new",
        attachments: list | None = None,
        continue_url: str | None = None,
        **kwargs,
    ) -> dict:
        t0 = time.monotonic()
        try:
            ok = await self.ensure_authenticated()
            if not ok:
                return {
                    "ok": False,
                    "error": "Authentication failed — check authchatgpt.json credentials",
                    "account": self.account,
                }

            response = await self.send_prompt(
                prompt, mode=mode, attachments=attachments, continue_url=continue_url,
            )
            final_text = await self._extract_response(prompt) or response
            code_blocks = self.extract_code_blocks(final_text)

            result = {
                "ok": True,
                "text": final_text,
                "code_blocks": code_blocks or None,
                "usage": {
                    "prompt_tokens": _count_tokens(prompt),
                    "completion_tokens": _count_tokens(final_text),
                },
                "account": self.account,
                "conversation_url": self._page.url if self._page else None,
                "mode": mode,
                "elapsed": time.monotonic() - t0,
            }
            try:
                self.save_to_json(result)
            except Exception:
                pass
            return result
        except Exception as exc:
            await self.take_debug_screenshot("scrape_error")
            return {"ok": False, "error": str(exc), "account": self.account}

    def _extra_send_kwargs(self) -> dict:
        return {}
