"""
scrapers/grok_scraper.py — GrokScraper(BaseAIChatScraper).

This REPLACES scrapers/chatgpt_scraper.py. Implements the Grok-specific
chat flow, ported 1:1 from the owner-supplied reference scraper
(grok.js / grokdebug.js), while REUSING the ChatGPT backend's proven
Cloudflare/CDP mitigation ladder from scrapers/base_grok.py unchanged.

Auth model (the one deliberate, load-bearing deviation from ChatGPT):
  grok.com authenticates via SSO (Google / X / Apple / email-link) with no
  stable "email → Continue → password" form to automate — the reference
  scraper (login-capture-grok.js) never tries to automate it either; it
  only ever captures cookies from a login performed entirely by hand.
  ensure_authenticated() here therefore NEVER attempts to fill a login
  form. It only checks whether the persisted profile session is already
  valid and, if not, fails loud with instructions to run login_grok.py
  (one-time visible manual login) or import_grok_cookies.py (cookie
  injection, zero automation) — mirroring layers 4/5 of the ChatGPT
  backend's mitigation ladder exactly.

Chat flow (ported from grok.js's scrapeGrok()):
  NEW:      goto new_chat_url → fill prompt → send → wait_for_response
  CONTINUE: goto saved conversation_url (from SessionStore, passed via the
            `continue_url` kwarg) → fill prompt → send → wait_for_response

Response-choice A/B dialog ("Which response do you prefer?"): a real Grok
UI quirk (ported from grok.js's handleResponseChoice()) not present on
ChatGPT — detected right after sending and resolved (random pick, or Skip)
before polling for the final response.

Response extraction: last message-item element inside the main chat area
→ innerText(), then cleaned with the same regex passes grok.js uses to
strip UI chrome (Thinking…/search-status lines, image credits, timestamps,
speed labels, copy/share buttons, suggested follow-ups).

Canvas/code extraction: grok.js additionally scans `pre, code,
[class*="code"], [class*="canvas"]` elements (not just ``` fenced blocks
in the response text) and saves each as a standalone "canvas" file with
content-hash de-duplication — ported here as `_extract_canvas_blocks()`.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from config import GROK_AUTH_CONFIG, GROK_CONFIG
from scrapers.base_grok import BaseAIChatScraper, _count_tokens

SEL = GROK_CONFIG["selectors"]


class GrokScraper(BaseAIChatScraper):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Set once a NEW chat has actually been opened / a CONTINUE goto has
        # landed on a conversation — mirrors DeepSeek/ChatGPT's pattern.
        self._conversation_started: bool = False

    # ── Small selector-list helpers (pola existing) ─────────────────── #

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

    # ── B1. ensure_authenticated() — NO automated login (see module
    #        docstring); only checks the existing session. ──────────── #

    async def ensure_authenticated(self) -> bool:
        if self._page is None:
            await self.launch_browser(account=self.account)
        assert self._page is not None

        try:
            current_url = self._page.url or ""
            if "grok.com" not in current_url:
                await self._page.goto(
                    GROK_CONFIG["base_url"], wait_until="domcontentloaded", timeout=30_000,
                )
                await asyncio.sleep(1.5)  # beri waktu SPA render
        except Exception as nav_err:
            self.logger.warning("ensure_authenticated: gagal navigasi: %s", nav_err)

        if await self._is_logged_in():
            self.logger.info(
                "ensure_authenticated: session account '%s' masih valid ✅", self.account,
            )
            self._authenticated = True
            return True

        if await self._check_captcha(self._page):
            pass  # already logged/screenshotted by _check_captcha

        self.logger.error(
            "ensure_authenticated: account '%s' tidak memiliki session valid, dan "
            "backend Grok TIDAK melakukan automated login (grok.com adalah SSO-only "
            "— lihat GROK_BACKEND.md §Auth). Jalankan salah satu:\n"
            "  python login_grok.py --account %s          (visible, login manual sekali)\n"
            "  python import_grok_cookies.py --account %s --cookies <export.json>  "
            "(zero-automation, dari cookie export)",
            self.account, self.account, self.account,
        )
        self._authenticated = False
        return False

    async def _check_captcha(self, page: Page) -> bool:
        """Jika captcha/Turnstile terdeteksi → log + screenshot. Returns True
        if a captcha was detected (caller should treat auth as failed).
        REUSED VERBATIM from the ChatGPT backend's mitigation policy."""
        if await self._has_visible(page, SEL["captcha"], timeout_ms=500):
            self.logger.error(
                "Captcha/Cloudflare Turnstile terdeteksi di halaman Grok. Jalankan "
                "login_grok.py sekali dengan browser visible dan selesaikan manual; "
                "profile akan mengingat session."
            )
            await self.take_debug_screenshot("grok_captcha")
            return True
        return False

    # ── B2. send_prompt(prompt, mode, attachments) — flow chat ─────── #

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
                GROK_CONFIG["new_chat_url"], wait_until="domcontentloaded", timeout=30_000,
            )
            await asyncio.sleep(0.8)
            self._conversation_started = False

        textarea = await self._find_first(self._page, SEL["prompt_textarea"], timeout_ms=15_000)
        if not textarea:
            await self.take_debug_screenshot("prompt_textarea_not_found")
            raise RuntimeError("Prompt input tidak ditemukan pada halaman Grok")

        if attachments:
            await self._upload_attachments(attachments)

        try:
            await textarea.click()
            await textarea.fill(prompt)
        except Exception:
            # Fallback: type per-character (ported from grok.js's fallback path).
            try:
                await textarea.click()
                await textarea.type(prompt, delay=10)
            except Exception as exc:
                await self.take_debug_screenshot("fill_prompt_failed")
                raise RuntimeError(f"Failed to fill prompt: {exc}") from exc

        await asyncio.sleep(GROK_CONFIG["timeouts"]["between_actions"] / 1000)

        sent = await self._click_send_button(self._page)
        if not sent:
            try:
                await textarea.press("Enter")
            except Exception as exc:
                await self.take_debug_screenshot("send_prompt_failed")
                raise RuntimeError(f"Failed to send prompt: {exc}") from exc

        await asyncio.sleep(0.5)

        # Ported from grok.js's initial post-send wait (varies by mode).
        await asyncio.sleep(2.0 if mode != "continue" else 6.0)

        # Grok-specific: "Which response do you prefer?" A/B dialog.
        await self._handle_response_choice()

        response = await self.wait_for_response()
        return response

    async def _click_send_button(self, page: Page) -> bool:
        """Try the configured selectors first, then fall back to a text/
        aria-label scan over every button/[role=button] — ported from
        grok.js, which has no stable send-button selector on grok.com."""
        if await self._click_first(page, SEL["send_button"], timeout_ms=3000):
            return True
        try:
            clicked = await page.evaluate(
                """
                () => {
                    const buttons = document.querySelectorAll('button, [role="button"]');
                    for (const btn of buttons) {
                        const text = (btn.innerText || btn.textContent || '').toLowerCase();
                        const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                        if (text.includes('send') || ariaLabel.includes('send')) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
                """
            )
            return bool(clicked)
        except Exception:
            return False

    async def _handle_response_choice(self) -> bool:
        """Grok occasionally shows a "Which response do you prefer?" A/B
        dialog. Ported 1:1 from grok.js's handleResponseChoice(): wait for
        both candidates to finish "thinking", then randomly pick one (or
        click Skip/Lewati as a fallback)."""
        assert self._page is not None
        try:
            body_text = (await self._page.inner_text("body", timeout=2_000)).lower()
        except Exception:
            return False

        has_dialog = any(
            marker.lower() in body_text for marker in SEL["response_choice_dialog_text"]
        )
        if not has_dialog:
            return False

        self.logger.info("Response-choice A/B dialog terdeteksi — menunggu kandidat selesai…")
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                still_thinking = re.search(
                    r"sedang berpikir|thinking|berfikir",
                    await self._page.inner_text("body", timeout=2_000),
                    re.IGNORECASE,
                )
            except Exception:
                still_thinking = None
            if not still_thinking:
                break
            await asyncio.sleep(0.5)

        import random
        choice = random.choice(SEL["response_choice_buttons"])
        clicked = await self._click_first(self._page, [f'button:has-text("{choice}")'], timeout_ms=3000)
        if clicked:
            self.logger.info("Response choice dipilih secara acak: %s", choice)
            await asyncio.sleep(2.0)
            return True

        skipped = await self._click_first(self._page, SEL["response_choice_skip"], timeout_ms=2000)
        if skipped:
            self.logger.info("Response-choice dialog di-skip (Lewati/Skip)")
            await asyncio.sleep(1.0)
        return skipped

    # ── B3. Response extraction ─────────────────────────────────────── #

    async def _extract_response(self, prompt: str) -> str:
        assert self._page is not None
        raw = await self._extract_current_text()
        return self._clean_response_text(raw, prompt)

    # Ported 1:1 from grok.js's response-cleaning regex cascade.
    _UI_LINE_PATTERNS = [
        r"^(fast|slow|auto|standard|copy|share|send|stop|jelaskan|soal|pikir|buat|terjemahkan)(\s|$)",
        r"^\d+[,\.]\d*\s*s$",
        r"^[a-z0-9.-]+\.(com|org|net|io|co\.uk|gov)$",
    ]
    _UI_LINE_RE = re.compile("|".join(_UI_LINE_PATTERNS), re.IGNORECASE)

    def _clean_response_text(self, text: str, prompt: str) -> str:
        if not text:
            return ""
        cleaned = text

        # Strip the user's own prompt if it leaked into the captured block.
        if prompt and prompt.strip() in cleaned:
            cleaned = cleaned.replace(prompt.strip(), "", 1)

        patterns = [
            (r".*?(gettyimages|unsplash|pexels|pixabay|wikimedia|imgur|pinterest)\.com[^\n]*", ""),
            (r"^(Photo|Image|Source|Credit|Credits|Attribution):?\s+[^\n]*", ""),
            (r"Slide\s+(berikutnya|sebelumnya|next|previous|lainnya)", ""),
            (r"^(<<|>>|\d+/\d+|[\d→←])\s*$", ""),
            (r"Membaca\s*postingan", ""),
            (r"Menelusuri\s*web", ""),
            (r"Mencari\s*informasi", ""),
            (r"Menganalisis", ""),
            (r"Reading\s*posts", ""),
            (r"Browsing\s*web", ""),
            (r"Searching\s*for", ""),
            (r"Analyzing", ""),
            (r"\d+[,\.]\d*\s*s\s*$", ""),
            (r"^\s*\d+\s*s\s*$", ""),
            (r"\b(Fast|Slow|Standard|Auto)\b\s*$", ""),
            (r"Thinking.*?\.{3}", ""),
            (r"Stop responding", ""),
            (r"Copy\s*Share this response\s*Send feedback", ""),
            (r"Share this response\s*Send feedback", ""),
            (r"Pikir lebih keras\s*$", ""),
        ]
        for pat, repl in patterns:
            cleaned = re.sub(pat, repl, cleaned, flags=re.IGNORECASE | re.MULTILINE)

        lines = []
        for ln in cleaned.splitlines():
            trimmed = ln.strip()
            if not trimmed:
                continue
            if self._UI_LINE_RE.match(trimmed):
                continue
            lines.append(trimmed)

        result = "\n".join(lines)
        result = re.sub(r"\n\s*\n+", "\n", result).strip()
        return result

    # ── B4. Canvas / code-block extraction (grok.js saveCanvasContent) ─ #

    async def _extract_canvas_blocks(self) -> list[dict]:
        """Scan `pre, code, [class*="code"], [class*="canvas"]` elements
        (not just ``` fenced blocks in the plain-text response) and return
        de-duplicated, size-filtered canvas snippets — ported 1:1 from
        grok.js's saveCanvasContent()/hashContent() pipeline."""
        assert self._page is not None
        try:
            nodes = await self._page.query_selector_all(
                'pre, code, [class*="code"], [class*="canvas"]'
            )
        except Exception:
            return []

        seen_hashes: set[str] = set()
        blocks: list[dict] = []
        for node in nodes[:20]:
            try:
                content = ((await node.inner_text()) or "").strip()
            except Exception:
                continue
            if len(content) < 50:
                continue
            # Same UI-label stripping grok.js applies before hashing.
            content = re.sub(
                r"^(Salin|HTML|JavaScript|CSS|Python|Json)\s*$", "", content,
                flags=re.IGNORECASE | re.MULTILINE,
            ).strip()
            if len(content) < 50:
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            lang = self.detect_file_type(content)
            blocks.append({"lang": lang, "code": content})
        return blocks

    # ── B5. _upload_attachments(attachments) ────────────────────────── #

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
            fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="grok_attach_")
            os.close(fd)
            Path(tmp_path).write_bytes(data)

            try:
                file_input = await self._find_first(self._page, SEL["file_input"], timeout_ms=5000)
                if not file_input:
                    raise RuntimeError("File input tidak ditemukan pada halaman Grok")
                await file_input.set_input_files(tmp_path)

                ok = await self._has_visible(
                    self._page, SEL["attachment_preview"],
                    timeout_ms=GROK_CONFIG["timeouts"]["attachment_preview"],
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

    # ── B6. scrape() — orkestrasi + hasil ───────────────────────────── #

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
                    "error": (
                        "Authentication failed — no valid Grok session for this account. "
                        "Run login_grok.py or import_grok_cookies.py first (see GROK_BACKEND.md)."
                    ),
                    "account": self.account,
                    "status": 401,
                }

            response = await self.send_prompt(
                prompt, mode=mode, attachments=attachments, continue_url=continue_url,
            )
            final_text = await self._extract_response(prompt) or response
            canvas_blocks = await self._extract_canvas_blocks()
            code_blocks = self.extract_code_blocks(final_text) or None

            current_url = self._page.url if self._page else None
            conversation_id = None
            if current_url:
                m = re.search(r"c/([a-zA-Z0-9\-]+)", current_url)
                if m:
                    conversation_id = m.group(1)

            result = {
                "ok": True,
                "text": final_text,
                "code_blocks": code_blocks,
                "canvas": canvas_blocks or None,
                "conversation_id": conversation_id,
                "usage": {
                    "prompt_tokens": _count_tokens(prompt),
                    "completion_tokens": _count_tokens(final_text),
                },
                "account": self.account,
                "conversation_url": current_url,
                "mode": mode,
                "elapsed": time.monotonic() - t0,
            }
            if canvas_blocks:
                try:
                    self.save_code_files(
                        [{"index": i + 1, "lang": b["lang"],
                          "extension": {"python": "py", "javascript": "js", "html": "html"}.get(b["lang"], "txt"),
                          "code": b["code"]} for i, b in enumerate(canvas_blocks)],
                        prefix="canvas",
                    )
                except Exception:
                    pass
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
