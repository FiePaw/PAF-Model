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
from scrapers import tool_protocol as tp
from scrapers.base_grok import BaseAIChatScraper, _count_tokens

SEL = GROK_CONFIG["selectors"]


class GrokScraper(BaseAIChatScraper):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Set once a NEW chat has actually been opened / a CONTINUE goto has
        # landed on a conversation — mirrors DeepSeek/ChatGPT's pattern.
        self._conversation_started: bool = False
        # Per-request protocol context (set by scrape()): the function list the
        # client declared and the advisory max_tokens hint.
        self._tools: list | None = None
        self._max_tokens: int | None = None

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

        # STALE-HANDLE FIX: never reuse the ElementHandle from _find_first()
        # across actions — React re-renders (hydration, attachment upload,
        # banner dismiss) detach it and Playwright raises
        # "Element is not attached to the DOM". _fill_prompt() re-resolves a
        # FRESH locator on every attempt (locators re-query the DOM on each
        # action, so they cannot go stale).
        await self._fill_prompt(prompt)

        await asyncio.sleep(GROK_CONFIG["timeouts"]["between_actions"] / 1000)

        sent = await self._click_send_button(self._page)
        if not sent:
            # Fresh-locator Enter fallback (stale-handle fix — the old code
            # pressed Enter on the SAME possibly-detached ElementHandle).
            if not await self._press_enter_fresh():
                await self.take_debug_screenshot("send_prompt_failed")
                raise RuntimeError(
                    "Failed to send prompt: send button not clicked and Enter "
                    "rejected by all composer selectors"
                )

        # PRE-RESPONSE BASELINE (Bug #5): capture the last assistant message
        # RIGHT AFTER sending — at this instant the DOM still shows the
        # PREVIOUS turn (mode='continue' goto lands on the old answer, and
        # even mode='new' can restore the last conversation — both
        # live-confirmed via debug_grok_div.py). wait_for_response() treats
        # text equal to this baseline as "not updated yet", so the old
        # answer can never be returned as the response to the new prompt.
        try:
            pre_send_text = await self._extract_current_text()
        except Exception:
            pre_send_text = ""

        await asyncio.sleep(0.5)

        # Ported from grok.js's initial post-send wait (varies by mode).
        await asyncio.sleep(2.0 if mode != "continue" else 6.0)

        # Grok-specific: "Which response do you prefer?" A/B dialog.
        await self._handle_response_choice()

        response = await self.wait_for_response(pre_send_text=pre_send_text)
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
        r"^\d+[,\.)]\d*\s*s$",
        # Grok status/footer chrome (live-confirmed via debug_grok_div.py —
        # the assistant bubble carries "Worked for 5s" above the answer and
        # a "10 sources" web-search footer below it):
        r"^worked\s+for\s+[\dhms.,\s]+$",   # "Worked for 5s" / "Worked for 2m 30s"
        r"^\d+\s+sources?$",                # "10 sources" / "1 source"
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
        tools: list | None = None,
        max_tokens: int | None = None,
        tool_messages: list | None = None,
        next_user_msg: str | None = None,
        system_prompt: str | None = None,
        **kwargs,
    ) -> dict:
        """Run one chat turn.

        Shared protocol (scrapers/tool_protocol.py), same as DeepSeek/Qwen:
          * the prompt is wrapped in [SYSTEM CONTEXT]/[USER REQUEST]; a
            client-supplied ``system_prompt`` is embedded in [SYSTEM CONTEXT]
            on every turn (new, continue and tool-result turns);
          * a normal reply is plain text (cleaned of Grok UI chrome);
          * a function call is an @@PAF-CALL:<name>@@ … @@PAF-END@@ block,
            parsed from the RAW text (the UI-cleaning regexes would corrupt
            argument values: they drop blank lines and trim indentation) and
            returned as OpenAI ``tool_calls``;
          * tool_messages (+ optional next_user_msg) = Turn 2: hand function
            results back to the model in the same conversation (mode must be
            "continue").
        """
        t0 = time.monotonic()
        self._tools = tools
        self._max_tokens = max_tokens
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

            if tool_messages:
                outgoing = tp.build_tool_result_prompt(
                    tool_messages, next_user_msg, tools=tools,
                    client_system=system_prompt, max_tokens=max_tokens,
                )
            else:
                outgoing = tp.build_wrapped_prompt(
                    prompt, tools=tools, client_system=system_prompt,
                    max_tokens=max_tokens,
                )

            def _parse(txt: str) -> "tp.ParsedOutput":
                return tp.parse_model_output(
                    txt, tools=tools, expect_calls=bool(tool_messages),
                )

            response = await self.send_prompt(
                outgoing, mode=mode, attachments=attachments, continue_url=continue_url,
            )
            out = _parse(response)

            # Broken function-call block / empty reply → short correction in the
            # SAME conversation. Needs a real /c/<id> URL to re-enter the thread.
            corrections = 0
            while out.kind == tp.KIND_INVALID and corrections < tp.MAX_CORRECTIVE_RETRIES:
                conv_url = self._page.url if self._page else None
                if not conv_url or "/c/" not in conv_url:
                    break
                corrections += 1
                self.logger.warning(
                    "Reply invalid (corrective %d/%d): %s | raw[:200]=%r",
                    corrections, tp.MAX_CORRECTIVE_RETRIES, out.error, (response or "")[:200],
                )
                response = await self.send_prompt(
                    tp.corrective_prompt_for(out), mode="continue", continue_url=conv_url,
                )
                out = _parse(response)

            if out.kind == tp.KIND_INVALID:
                return {
                    "ok": False,
                    "error": f"invalid_response: {out.error}",
                    "account": self.account,
                    "raw": (response or "")[:2000],
                }

            tool_calls = None
            if out.kind == tp.KIND_TOOL_CALLS:
                tool_calls = out.tool_calls
                final_text = ""
                canvas_blocks: list = []
                code_blocks = None
            else:
                if out.legacy:
                    final_text = out.text
                else:
                    final_text = await self._extract_response(outgoing) or out.text
                canvas_blocks = await self._extract_canvas_blocks()
                code_blocks = self.extract_code_blocks(final_text) or None

            current_url = self._page.url if self._page else None
            conversation_id = None
            if current_url:
                m = re.search(r"c/([a-zA-Z0-9\-]+)", current_url)
                if m:
                    conversation_id = m.group(1)

            prompt_tokens = _count_tokens(outgoing)
            completion_tokens = _count_tokens(final_text or response or "")
            result = {
                "ok": True,
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "text": final_text,
                "code_blocks": code_blocks,
                "canvas": canvas_blocks or None,
                "conversation_id": conversation_id,
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
                "account": self.account,
                "conversation_url": current_url,
                "mode": mode,
                "elapsed": time.monotonic() - t0,
            }
            if tool_calls:
                result["tool_calls"] = tool_calls
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
