"""
QwenScraper – concrete scraper for https://chat.qwen.ai
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import tempfile
from pathlib import Path
from typing import Literal

from config import QWEN_AUTH_CONFIG, QWEN_CONFIG, ROTATION_CONFIG
from scrapers.base_qwen import BaseAIChatScraper
from scrapers.utils import contains_any, discover_cookie_files


# ─── Attachment helper types ──────────────────────────────────────────────────

class Attachment:
    """
    Representasi satu file attachment yang akan diupload ke Qwen.

    Bisa dibuat dari:
      • Path file lokal  : Attachment.from_path("/path/to/image.png")
      • Base64 string    : Attachment.from_base64("data:image/png;base64,iVBOR...", "image.png")
                           atau Attachment.from_base64("iVBOR...", "image.png", "image/png")

    Attribute:
      name      – nama file (ditampilkan di UI Qwen)
      data      – bytes isi file
      mime_type – MIME type, misalnya "image/png"
    """

    # Tipe file yang didukung Qwen (untuk validasi ringan)
    SUPPORTED_MIME_PREFIXES = (
        "image/",          # jpg, png, webp, gif, bmp, tiff, svg, ico
        "application/pdf",
        "text/",           # txt, csv, html, xml, markdown, dll
        "application/json",
        "application/msword",
        "application/vnd.openxmlformats-officedocument",   # docx, xlsx, pptx
        "application/vnd.ms-",                              # xls, ppt
        "audio/",
        "video/",
    )

    def __init__(self, name: str, data: bytes, mime_type: str) -> None:
        self.name = name
        self.data = data
        self.mime_type = mime_type

    # ── Factory methods ───────────────────────────────────────────────────────

    @classmethod
    def from_path(cls, path: str | Path) -> "Attachment":
        """Buat Attachment dari path file lokal."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File tidak ditemukan: {p}")
        mime, _ = mimetypes.guess_type(str(p))
        if not mime:
            mime = "application/octet-stream"
        return cls(name=p.name, data=p.read_bytes(), mime_type=mime)

    @classmethod
    def from_base64(
        cls,
        b64_data: str,
        filename: str,
        mime_type: str | None = None,
    ) -> "Attachment":
        """
        Buat Attachment dari string base64.

        b64_data bisa berupa:
          • Data URI: "data:image/png;base64,iVBOR..."
          • Raw base64: "iVBOR..."

        mime_type opsional; jika tidak diisi akan di-guess dari filename
        atau diambil dari Data URI prefix.
        """
        if b64_data.startswith("data:"):
            # Format: "data:<mime>;base64,<data>"
            header, _, raw = b64_data.partition(",")
            if not mime_type:
                # Ambil MIME dari header "data:image/png;base64"
                mime_part = header[5:]  # buang "data:"
                mime_type = mime_part.split(";")[0]
            b64_data = raw

        # Padding fix
        b64_data = b64_data.strip()
        missing = len(b64_data) % 4
        if missing:
            b64_data += "=" * (4 - missing)

        data = base64.b64decode(b64_data)

        if not mime_type:
            guessed, _ = mimetypes.guess_type(filename)
            mime_type = guessed or "application/octet-stream"

        return cls(name=filename, data=data, mime_type=mime_type)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def is_supported(self) -> bool:
        return any(self.mime_type.startswith(p) for p in self.SUPPORTED_MIME_PREFIXES)

    def to_temp_file(self) -> Path:
        """
        Tulis data ke file sementara dan return Path-nya.
        Caller bertanggung jawab menghapus file setelah selesai.
        """
        suffix = Path(self.name).suffix or mimetypes.guess_extension(self.mime_type) or ".bin"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="qwen_attach_")
        import os
        os.close(fd)
        Path(tmp_path).write_bytes(self.data)
        return Path(tmp_path)

    def __repr__(self) -> str:
        return f"<Attachment name={self.name!r} mime={self.mime_type} size={len(self.data)}B>"


_TIMEOUT = QWEN_CONFIG["timeouts"]
_SELECTORS = QWEN_CONFIG["selectors"]
_THINK_LABELS = QWEN_CONFIG["think_mode_labels"]

ThinkMode = Literal["auto", "thinking", "fast"]


class QwenScraper(BaseAIChatScraper):
    """Scraper for Qwen AI chat (chat.qwen.ai)."""

    BASE_URL: str = QWEN_CONFIG["base_url"]

    _SEL_TEXTAREA = "textarea[placeholder], div[contenteditable='true']"
    _SEL_SEND_BTN = (
        "button[aria-label*='send' i], "
        "button[class*='send' i], "
        "button[data-testid*='send' i], "
        "button[class*='ant-btn'][class*='primary']:not([disabled]), "
        "button[class*='submit' i], "
        "button[type='submit']"
    )
    _SEL_STOP_BTN = "button[aria-label*='stop' i], button[class*='stop' i]"
    _SEL_THINKING = ".thinking-indicator, .loading-dots, [class*='thinking']"

    def __init__(
        self,
        headless: bool = True,
        cookies_path: Path | str | None = None,
        cookies_dir: Path | str | None = None,
        think_mode: ThinkMode | None = None,
        # ── Email+password auth (new, mirrors DeepSeek) ────────────────────
        account: str | None = None,
        email: str | None = None,
        password: str | None = None,
    ) -> None:
        super().__init__(
            headless=headless,
            cookies_path=cookies_path,
            cookies_dir=cookies_dir,
            account=account,
            email=email,
            password=password,
        )
        self._conversation_started = False
        self._last_prompt = ""
        # Use provided think_mode or fall back to config default
        self._think_mode: ThinkMode = think_mode or QWEN_CONFIG["default_think_mode"]
        self._think_mode_applied = False   # reset when a new chat page is loaded
        # max_tokens per-request, diisi dari luar (mis. newpublic_BETA.py) sebelum
        # scrape()/send_prompt() dipanggil — ikut dikirim di payload [USER REQUEST].
        self._max_tokens: int | None = None
        # tools: list of OpenAI-compatible tool definitions, diisi dari luar.
        # Jika None → mode chat biasa (tidak ada tool calling).
        # Jika diisi → mode LLM API dengan tool calling.
        self._tools: list[dict] | None = None

    # ── Context manager override ──────────────────────────────────────────────

    async def __aenter__(self) -> "QwenScraper":
        return await super().__aenter__()

    def _extra_send_kwargs(self) -> dict:
        return {"think_mode": self._think_mode}

    # ── Navigation ────────────────────────────────────────────────────────────

    async def _goto_new_chat(self) -> None:
        self.logger.info("Navigating to Qwen AI: %s", self.BASE_URL)
        await self._page.goto(
            self.BASE_URL, wait_until="domcontentloaded", timeout=_TIMEOUT["page_load"]
        )
        
        # Tunggu input field siap (bukan sleep blind)
        try:
            await self._page.wait_for_selector(
                self._SEL_TEXTAREA, 
                timeout=_TIMEOUT["between_actions"] * 3
            )
            await self._page.wait_for_load_state("networkidle", timeout=100)
        except Exception as e:
            self.logger.warning("Timeout waiting for input ready: %s", e)

        # Cek apakah halaman crash setelah navigate
        if await self._is_page_crashed():
            self.logger.warning("⚠️  Page crash terdeteksi setelah navigate ke Qwen – perlu restart")
            raise RuntimeError("Page crashed after navigation to Qwen")
        
        self._conversation_started = False
        self._think_mode_applied = False
        self.logger.debug("Landed on new-chat page")

    async def _rotate_and_reset(self) -> bool:
        """Rotate account and reset think-mode state for the new session."""
        rotated = await self._rotate_account()
        if rotated:
            self._think_mode_applied = False
        return rotated

    async def _ensure_page_ready(self, mode: str) -> None:
        if mode == "new" or not self._conversation_started:
            await self._goto_new_chat()
        else:
            self.logger.debug("Continuing existing conversation")
            # Pada mode continue, think-mode trigger tidak selalu tersedia
            # (UI Qwen menyembunyikannya di dalam conversation page).
            # Tandai sudah applied agar tidak buang waktu mencari trigger
            # yang memang tidak ada, kecuali ada explicit override.
            if not self._think_mode_applied:
                self._think_mode_applied = True
                self.logger.debug(
                    "Continue mode: think-mode UI not available in conversation page – skipping"
                )

    # ── Authentication (email + password, mirip DeepSeek) ────────────────────

    async def _is_unauthenticated(self) -> bool:
        """Return True jika halaman menampilkan tombol Login atau Sign Up.

        Ini menandakan session belum ada atau sudah expired. Dua cara deteksi:
          1. URL mengandung pola login (e.g. /auth, /login)
          2. Tombol "Log in" / "Sign up" ada di DOM
        """
        if self._page is None:
            return True
        try:
            url = self._page.url or ""
            # Cek URL patterns
            for pat in QWEN_AUTH_CONFIG["login_url_patterns"]:
                if pat in url.lower():
                    return True
            # Cek keberadaan tombol Login/Sign Up di DOM (screenshot menunjukkan
            # tombol "Log in" di kanan atas dan "Sign up" di samping kanannya)
            for sel in QWEN_AUTH_CONFIG["unauthenticated_selectors"]:
                try:
                    el = await self._page.query_selector(sel)
                    if el and await el.is_visible():
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    async def ensure_authenticated(self) -> bool:
        """Idempotent auth check untuk Qwen.

        Flow:
          1. Jika profile session masih valid → selesai (return True).
          2. Jika terdeteksi tombol Login/Sign Up → arahkan ke login page
             dan login dengan email + password dari cookies/auth.json.
          3. Session berhasil → simpan ke persistent profile.
        """
        if self._page is None:
            return False

        # Jika sudah login, tidak perlu apa-apa
        if not await self._is_unauthenticated():
            self._authenticated = True
            return True

        self.logger.info(
            "Tombol Login/Sign Up terdeteksi untuk account '%s' → memulai login",
            self.account,
        )
        ok = await self.login()
        self._authenticated = ok
        return ok

    async def login(
        self,
        email: str | None = None,
        password: str | None = None,
    ) -> bool:
        """Login ke Qwen dengan email + password.

        Alur:
          1. Navigasi ke https://chat.qwen.ai/auth
          2. Isi form email + password
          3. Klik "Sign in"
          4. Tunggu redirect ke chat UI (session valid)
          5. Simpan cookies ke profile (persistent profile sudah menyimpannya)

        Jika captcha muncul: headless tidak bisa selesaikan →
        jalankan ulang dengan --no-headless sekali; profile akan mengingat sesi.
        """
        if self._page is None:
            await self.launch_browser(account=self.account)
        assert self._page is not None

        email, password = self._resolve_credentials(email, password)
        if not email or not password:
            self.logger.error(
                "Tidak ada credentials untuk account '%s'. "
                "Tambahkan ke cookies/auth.json, atau set env %s / %s.",
                self.account,
                QWEN_AUTH_CONFIG["env_email"],
                QWEN_AUTH_CONFIG["env_password"],
            )
            return False

        self.logger.info("Login ke Qwen sebagai %s (account: %s)", email, self.account)

        # Navigasi ke halaman login
        await self._page.goto(
            QWEN_AUTH_CONFIG["login_url"],
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await asyncio.sleep(1.5)

        login_sel = QWEN_CONFIG["selectors"]["login"]

        # Cari field email
        email_loc = None
        for sel in login_sel["email_input"]:
            try:
                el = await self._page.wait_for_selector(sel, timeout=6_000, state="visible")
                if el:
                    email_loc = el
                    break
            except Exception:
                continue

        # Cari field password
        pwd_loc = None
        for sel in login_sel["password_input"]:
            try:
                el = await self._page.wait_for_selector(sel, timeout=4_000, state="visible")
                if el:
                    pwd_loc = el
                    break
            except Exception:
                continue

        if email_loc is None or pwd_loc is None:
            self.logger.error(
                "Form login tidak ditemukan di %s. "
                "Mungkin sudah login atau selector perlu diperbarui.",
                QWEN_AUTH_CONFIG["login_url"],
            )
            # Mungkin sudah di-redirect ke chat
            return not await self._is_unauthenticated()

        # Isi email dan password
        try:
            await email_loc.click()
            await email_loc.fill(email)
            await asyncio.sleep(0.4)
            await pwd_loc.click()
            await pwd_loc.fill(password)
            await asyncio.sleep(0.6)
        except Exception as exc:
            self.logger.error("Gagal mengisi form login: %s", exc)
            return False

        # Klik tombol Sign in
        clicked = False
        for sel in login_sel["signin_button"]:
            try:
                btn = await self._page.wait_for_selector(sel, timeout=3_000, state="visible")
                if btn:
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            # Fallback: tekan Enter di field password
            self.logger.info("Tombol Sign in tidak ditemukan, mencoba Enter")
            try:
                await pwd_loc.press("Enter")
                clicked = True
            except Exception as exc:
                self.logger.error("Tidak bisa submit form: %s", exc)
                return False

        return await self._wait_for_login_result()

    async def _wait_for_login_result(self) -> bool:
        """Poll sampai login berhasil (chat UI muncul) atau gagal/timeout."""
        assert self._page is not None
        import time as _time_mod

        deadline = _time_mod.monotonic() + QWEN_AUTH_CONFIG["login_wait"]
        login_sel = QWEN_CONFIG["selectors"]["login"]

        while _time_mod.monotonic() < deadline:
            # Cek error inline (password salah, dll.)
            for sel in login_sel["error_message"]:
                try:
                    el = await self._page.query_selector(sel)
                    if el and await el.is_visible():
                        txt = (await el.inner_text()).strip()
                        if txt:
                            self.logger.error("Error login Qwen: %s", txt)
                            await self.take_debug_screenshot("login_error")
                            return False
                except Exception:
                    pass

            # Cek apakah sudah berhasil: tidak lagi di halaman auth DAN
            # input chat sudah muncul
            is_still_on_auth = await self._is_unauthenticated()
            if not is_still_on_auth:
                try:
                    # Konfirmasi: chat input sudah bisa dipakai
                    await self._page.wait_for_selector(
                        self._SEL_TEXTAREA, timeout=3_000, state="visible"
                    )
                    await asyncio.sleep(QWEN_AUTH_CONFIG["post_login_settle"])
                    self.logger.info(
                        "Login Qwen berhasil — session disimpan di profile '%s'",
                        self.account,
                    )
                    self._authenticated = True
                    # Simpan backup cookie (best-effort)
                    try:
                        await self.save_cookies()
                    except Exception:
                        pass
                    # Tandai profile sudah di-seed agar tidak dianggap fresh
                    profile_dir = self._profile_dir_for(account=self.account)
                    sentinel = profile_dir / "cookies_seeded"
                    if not sentinel.exists():
                        sentinel.write_text("1", encoding="utf-8")
                    return True
                except Exception:
                    pass  # chat input belum muncul, lanjut polling

            await asyncio.sleep(1.5)

        self.logger.error("Login Qwen timeout setelah %ss", QWEN_AUTH_CONFIG["login_wait"])
        await self.take_debug_screenshot("login_timeout")
        return False

    # ── Think mode ────────────────────────────────────────────────────────────

    async def _get_current_think_mode(self) -> str:
        """Read the currently active think mode label from the UI."""
        _CURRENT_MODE_CANDIDATES = [
            _SELECTORS["think_mode_selected"],       # .qwen-select-option-selected-label-container
            _SELECTORS["think_mode_trigger"],        # .qwen-select-thinking-label
            "[class*='thinking-label']",
            "[class*='think-mode-label']",
            "[class*='selected-label']",
        ]
        for sel in _CURRENT_MODE_CANDIDATES:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    text = (await el.inner_text()).strip().lower()
                    if text in ("auto", "thinking", "fast"):
                        print(f"Current think mode from selector '{sel}': {text}")  # debug
                        return text
                    for label in ("thinking", "fast", "auto"):
                        if label in text:
                            print(f"Current think mode from selector '{sel}': {text}")  # debug
                            return label
            except Exception:
                continue
        return ""

    async def debug_think_mode_selectors(self) -> dict:
        """
        Diagnostic helper: scan the page for any elements that might be
        related to the think-mode UI. Run with --no-headless to inspect.
        Returns a dict of selector → matched element info.
        """
        result = await self._page.evaluate("""
        () => {
            const knownLabels = ['auto', 'thinking', 'fast'];
            const found = [];
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_ELEMENT
            );
            let node;
            while ((node = walker.nextNode())) {
                const text = (node.innerText || '').trim().toLowerCase();
                if (knownLabels.includes(text) && node.offsetParent !== null) {
                    found.push({
                        tag: node.tagName,
                        className: node.className,
                        text: text,
                        id: node.id,
                        parentClass: node.parentElement
                            ? node.parentElement.className : '',
                    });
                }
            }
            return found;
        }
        """)
        self.logger.info("Think-mode debug scan found %d element(s): %s", len(result), result)
        return {"elements": result}

    async def _set_think_mode(self, mode: ThinkMode) -> bool:
        """
        Select *mode* in the Qwen think-mode dropdown.
        Returns True if the selection was applied (or already correct).

        Strategy (cascading fallbacks):
          1. Skip if the UI already shows the correct mode.
          2. Try multiple trigger selectors to open the dropdown.
          3. Wait briefly for the dropdown/popover to appear.
          4. Attempt to click the matching option via several selector patterns.
          5. JS brute-force fallback scanning all visible text nodes.
          6. Verify and mark as applied regardless (don't block scraping).
        """
        target_label = _THINK_LABELS.get(mode, mode).lower()

        # ── Step 1: skip if already correct ──────────────────────────────────
        current = await self._get_current_think_mode()
        if current and target_label in current:
            self.logger.debug("Think mode already '%s' – skipping", mode)
            self._think_mode_applied = True
            return True

        self.logger.info("Setting think mode → '%s'", mode)

        # ── Step 2: open the dropdown via multiple trigger candidates ─────────
        _TRIGGER_CANDIDATES = [
            # Confirmed class names seen on chat.qwen.ai
            ".qwen-select-thinking-label",
            #"[class*='thinking-label']",
            #"[class*='think-mode']",
            #"[class*='qwen-select']",
            # Generic fallbacks: buttons / divs near the textarea
            #"button[class*='think']",
            #"div[class*='think']",
            # Any element whose visible text matches known mode labels
        ]

        trigger_clicked = False
        for sel in _TRIGGER_CANDIDATES:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    trigger_clicked = True
                    self.logger.debug("Opened think-mode dropdown via selector: %s", sel)
                    #print(f"Clicked think-mode trigger: {sel}")
                    break
            except Exception:
                continue

        # JS fallback: find any visible element whose text is a known mode label
        if not trigger_clicked:
            trigger_clicked = await self._page.evaluate("""
            () => {
                const knownLabels = ['auto', 'thinking', 'fast'];
                const walk = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_ELEMENT
                );
                let node;
                while ((node = walk.nextNode())) {
                    const text = (node.innerText || '').trim().toLowerCase();
                    if (
                        knownLabels.includes(text) &&
                        node.offsetParent !== null &&
                        !['INPUT','TEXTAREA'].includes(node.tagName)
                    ) {
                        node.click();
                        return true;
                    }
                }
                return false;
            }
            """)
            if trigger_clicked:
                self.logger.debug("Opened think-mode dropdown via JS label scan")

        if not trigger_clicked:
            self.logger.warning("Could not locate think-mode trigger – skipping mode set")
            return False

        await asyncio.sleep(0.5)  # allow dropdown animation to complete

        # ── Step 3 & 4: find and click the target option ──────────────────────
        _OPTION_SELECTORS = [
            # rc-select virtual list (Ant Design)
            ".rc-virtual-list-holder-inner > *",
            ".rc-virtual-list-holder-inner li",
            # Ant Design Select options
            #".ant-select-item",
            #".ant-select-item-option",
            # Generic dropdown patterns
            #"[class*='option-item']",
            #"[class*='select-option']",
            #"[class*='dropdown-item']",
            #"[role='option']",
            #"[role='menuitem']",
        ]

        clicked = False
        for sel in _OPTION_SELECTORS:
            try:
                items = await self._page.query_selector_all(sel)
                for item in items:
                    try:
                        if not await item.is_visible():
                            continue
                        text = (await item.inner_text()).strip().lower()
                        if target_label in text:
                            await item.click()
                            clicked = True
                            #print(f"Clicked option '{text}' via selector: {sel}")
                            self.logger.debug("Clicked option '%s' via selector: %s", text, sel)
                            break
                    except Exception:
                        continue
                if clicked:
                    break
            except Exception:
                continue

        # ── Step 5: JS brute-force scan all visible text nodes ────────────────
        if not clicked:
            clicked = await self._page.evaluate(f"""
            () => {{
                const target = '{target_label}';
                // Prefer elements with role=option or inside a listbox/popover
                const candidates = [
                    ...document.querySelectorAll('[role="option"],[role="menuitem"],[role="listitem"]'),
                    ...document.querySelectorAll('[class*="option"],[class*="item"],[class*="list"] > *'),
                ];
                for (const el of candidates) {{
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (text === target && el.offsetParent !== null) {{
                        el.click();
                        return true;
                    }}
                }}
                // Wider scan: any element whose exact text matches
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_ELEMENT
                );
                let node;
                while ((node = walker.nextNode())) {{
                    const text = (node.innerText || '').trim().toLowerCase();
                    if (text === target && node.offsetParent !== null &&
                        !['INPUT','TEXTAREA','BODY','HTML'].includes(node.tagName)) {{
                        node.click();
                        return true;
                    }}
                }}
                return false;
            }}
            """)
            if clicked:
                self.logger.debug("Clicked option via JS brute-force scan")

        if not clicked:
            self.logger.warning("Could not click think mode option '%s' – proceeding anyway", mode)
            # Don't return False; mark applied so we don't retry on every prompt
            self._think_mode_applied = True
            return False

        await asyncio.sleep(0.4)

        # ── Step 6: verify ────────────────────────────────────────────────────
        new_label = await self._get_current_think_mode()
        if target_label in new_label:
            self.logger.info("Think mode confirmed → '%s' ✓", mode)
        else:
            self.logger.warning(
                "Think mode may not have applied (expected '%s', got '%s') – proceeding",
                target_label, new_label,
            )
        self._think_mode_applied = True
        return True

    # ── Input ─────────────────────────────────────────────────────────────────

    async def _find_input(self):
        candidates = [
            "textarea[placeholder]",
            "textarea#chat-input",
            "textarea[data-testid='chat-input']",
            "div[contenteditable='true'][data-testid]",
            "div[contenteditable='true']",
            "textarea",
        ]
        for sel in candidates:
            try:
                el = await self._page.wait_for_selector(sel, timeout=1_000, state="visible")
                if el:
                    self.logger.debug("Found input: %s", sel)
                    return el
            except Exception:
                continue
        raise RuntimeError("Could not locate chat input field")

    @staticmethod
    def _fill_timeout(text: str, base_ms: int = 1_000, cps: int = 1_000) -> int:
        """
        Hitung timeout fill() secara dinamis berdasarkan panjang teks.

        Rumus: base_ms + (jumlah_chars / cps) * 1000
          - base_ms : waktu minimum dalam ms (default 1000ms)
          - cps      : estimasi chars per second yang bisa di-fill Playwright (default 1000)

        Contoh hasil:
          -    500 chars →  1_000 ms  (tidak ada penalty)
          -  5_000 chars →  6_000 ms
          - 58_000 chars → 59_000 ms
        """
        return base_ms + (len(text) // cps) * 1_000

    async def _find_send_button(self):
        candidates = [
            self._SEL_SEND_BTN,
            "button[data-testid='send-button']",
            "button[aria-label='Send message']",
            "button[aria-label='Send']",
            "button[type='submit']",
        ]
        for sel in candidates:
            try:
                el = await self._page.query_selector(sel)
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _find_send_button_enabled(self, max_wait: float = 5.0):
        """
        Cari send button yang tidak disabled.
        Tunggu hingga max_wait detik jika tombol ditemukan tapi masih disabled
        (misalnya setelah input baru diisi, Qwen perlu sebentar untuk enable-nya).
        """
        candidates = [
            self._SEL_SEND_BTN,
            "button[data-testid='send-button']",
            "button[aria-label='Send message']",
            "button[aria-label='Send']",
            "button[type='submit']",
        ]
        deadline = asyncio.get_event_loop().time() + max_wait
        while asyncio.get_event_loop().time() < deadline:
            for sel in candidates:
                try:
                    el = await self._page.query_selector(sel)
                    if el:
                        disabled = await el.get_attribute("disabled")
                        aria_disabled = await el.get_attribute("aria-disabled")
                        if disabled is None and aria_disabled != "true":
                            return el
                except Exception:
                    continue
            await asyncio.sleep(0.2)
        # Kembalikan tombol apa pun yang ditemukan (meski disabled) atau None
        for sel in candidates:
            try:
                el = await self._page.query_selector(sel)
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _click_send_button(self, input_el, max_retries: int = 3) -> bool:
        """
        Klik send button dengan retry loop + re-query fresh setiap percobaan.

        Mengatasi error "Element is not attached to the DOM" yang terjadi saat
        SPA React me-re-render tombol di antara waktu query dan waktu click.

        Alur per attempt:
          1. Re-query send button fresh dari DOM (bukan pakai handle lama)
          2. Verifikasi elemen masih attached dengan is_visible()
          3. Coba click — jika sukses return True
          4. Jika NotAttached / detached → tunggu sebentar, ulangi
          5. Jika semua retry habis → fallback Enter key

        Returns True jika send button berhasil diklik, False jika pakai Enter fallback.
        """
        candidates = [
            self._SEL_SEND_BTN,
            "button[data-testid='send-button']",
            "button[aria-label='Send message']",
            "button[aria-label='Send']",
            "button[type='submit']",
        ]

        for attempt in range(1, max_retries + 1):
            # Re-query fresh setiap attempt — jangan pakai handle dari attempt sebelumnya
            send_btn = None
            for sel in candidates:
                try:
                    el = await self._page.query_selector(sel)
                    if not el:
                        continue
                    disabled      = await el.get_attribute("disabled")
                    aria_disabled = await el.get_attribute("aria-disabled")
                    if disabled is None and aria_disabled != "true":
                        send_btn = el
                        break
                except Exception:
                    continue

            if not send_btn:
                self.logger.debug(
                    "_click_send_button: attempt %d – send button tidak ditemukan, tunggu …",
                    attempt,
                )
                await asyncio.sleep(0.3 * attempt)
                continue

            try:
                # Verifikasi masih attached sebelum click
                if not await send_btn.is_visible():
                    self.logger.debug(
                        "_click_send_button: attempt %d – button tidak visible, re-query …",
                        attempt,
                    )
                    await asyncio.sleep(0.3 * attempt)
                    continue

                await send_btn.click()
                self.logger.debug(
                    "_click_send_button: klik berhasil pada attempt %d", attempt
                )
                return True

            except Exception as e:
                err_msg = str(e).lower()
                if "not attached" in err_msg or "detached" in err_msg or "element is not attached" in err_msg:
                    self.logger.warning(
                        "_click_send_button: attempt %d – button detached dari DOM, re-query … (%s)",
                        attempt, type(e).__name__,
                    )
                    await asyncio.sleep(0.3 * attempt)
                    continue
                else:
                    # Error lain (bukan detached) — log dan lanjut ke fallback
                    self.logger.warning(
                        "_click_send_button: attempt %d – error tidak terduga: %s",
                        attempt, e,
                    )
                    break

        # Semua retry habis → fallback bertingkat
        self.logger.warning(
            "_click_send_button: semua %d attempt gagal – mencoba fallback JS click …",
            max_retries,
        )

        # Fallback 1: klik via JavaScript (bypass visibility/attachment check)
        try:
            clicked_via_js = await self._page.evaluate("""
                () => {
                    const selectors = [
                        "button[aria-label*='send' i]",
                        "button[class*='send' i]",
                        "button[data-testid*='send' i]",
                        "button[class*='ant-btn'][class*='primary']:not([disabled])",
                        "button[class*='submit' i]",
                        "button[type='submit']"
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && !el.disabled) { el.click(); return true; }
                    }
                    return false;
                }
            """)
            if clicked_via_js:
                self.logger.warning("_click_send_button: JS click fallback berhasil")
                return False
        except Exception as js_err:
            self.logger.warning("_click_send_button: JS click fallback gagal: %s", js_err)

        # Fallback 2: Ctrl+Enter (lebih reliable dari Enter biasa di contenteditable)
        self.logger.warning("_click_send_button: fallback ke Ctrl+Enter …")
        try:
            await self._page.keyboard.press("Control+Enter")
            return False
        except Exception:
            pass

        # Fallback 3: Enter biasa (last resort)
        self.logger.warning("_click_send_button: last resort fallback ke Enter key")
        await input_el.press("Enter")
        return False

    # ── Response extraction ───────────────────────────────────────────────────

    async def _extract_last_response(self) -> str:
        text = await self._page.evaluate("""
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
                        '[class*="response-message"]',
                        '[class*="chat-response"]',
                        '[class*="assistant-message"]',
                        '[data-role="assistant"]',
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
        return (text or "").strip()

    async def _is_generating(self) -> bool:
        result = await self._page.evaluate("""
        () => {
            const stopSels = [
                'button[class*="stop"]',
                'button[aria-label*="stop" i]',
                'button[title*="stop" i]',
                '[class*="stop-btn"]',
                '[class*="abort"]',
            ];
            for (const s of stopSels) {
                const el = document.querySelector(s);
                if (el && el.offsetParent !== null) return true;
            }
            const streamSels = [
                '[class*="streaming"]',
                '[class*="typing"]',
                '[class*="loading-dots"]',
                '[class*="thinking-indicator"]',
                '[class*="cursor-blink"]',
                '.result-streaming',
            ];
            for (const s of streamSels) {
                const el = document.querySelector(s);
                if (el && el.offsetParent !== null) return true;
            }
            return false;
        }
        """)
        return bool(result)

    # ── Attachment upload ─────────────────────────────────────────────────────

    # ── Attachment upload via Clipboard ───────────────────────────────────────
    #
    # Kenapa clipboard?
    # Qwen menggunakan custom upload handler (bukan <input type="file"> standar).
    # File chooser dan set_input_files() gagal karena Playwright tidak bisa
    # intercept event handler milik Qwen. Clipboard paste adalah cara yang
    # paling reliable: kita inject ClipboardItem berisi file data langsung
    # ke clipboard browser via CDP (Chrome DevTools Protocol), lalu simulasikan
    # Ctrl+V ke textarea — persis seperti user melakukan copy-paste file.
    #
    # Urutan untuk SETIAP file:
    #   1. Encode file → base64
    #   2. Inject ke clipboard browser via CDP Page.addScriptToEvaluateOnNewDocument
    #      + Runtime.evaluate (setClipboardContents tidak perlu permission user)
    #   3. Fokus ke textarea
    #   4. Dispatch ClipboardEvent 'paste' dengan DataTransfer berisi file
    #   5. Tunggu preview/thumbnail muncul → konfirmasi berhasil
    #
    # Untuk non-image (PDF, txt, dll): gunakan DataTransfer dengan file blob,
    # bukan ImageBitmap — Qwen membaca tipe file dari DataTransfer.files[0].type.

    async def _upload_attachments(self, attachments: list[Attachment]) -> bool:
        """
        Upload semua attachment ke Qwen via clipboard paste (CDP).

        Mengembalikan True jika semua attachment berhasil di-paste,
        False jika ada yang gagal (scrape tetap dilanjutkan).
        """
        if not attachments:
            return True

        # Dapatkan CDP session untuk operasi clipboard
        cdp = await self._page.context.new_cdp_session(self._page)
        all_ok = True

        for att in attachments:
            if not att.is_supported():
                self.logger.warning(
                    "Attachment '%s' (MIME: %s) mungkin tidak didukung Qwen — tetap dicoba",
                    att.name, att.mime_type,
                )
            ok = await self._paste_attachment_via_cdp(cdp, att)
            if ok:
                # Jeda antar file agar Qwen sempat memproses upload sebelumnya
                await asyncio.sleep(1.2)
            else:
                all_ok = False
                self.logger.warning("Gagal paste attachment '%s'", att.name)

        await cdp.detach()
        return all_ok

    async def _paste_attachment_via_cdp(self, cdp, att: Attachment) -> bool:
        """
        Paste satu Attachment ke input Qwen menggunakan CDP Runtime.evaluate.

        Strategi:
        1. Encode data file ke base64.
        2. Inject JS ke halaman yang:
           a. Membuat Blob dari base64 data.
           b. Membuat File object dengan nama dan MIME type yang benar.
           c. Membuat DataTransfer dan masukkan File ke dalamnya.
           d. Dispatch ClipboardEvent 'paste' ke textarea/input yang aktif.
        3. Tunggu preview attachment muncul sebagai konfirmasi.
        """
        try:
            b64_data = base64.b64encode(att.data).decode("ascii")
            mime     = att.mime_type
            name     = att.name.replace("'", "\\'")   # escape untuk JS string

            self.logger.debug(
                "CDP paste: '%s' (%s, %d bytes)", att.name, mime, len(att.data),
            )

            # Fokus ke input area dulu agar paste event diterima
            await self._page.evaluate("""
            () => {
                const candidates = [
                    'textarea[placeholder]',
                    'textarea',
                    'div[contenteditable="true"]',
                ];
                for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (el) { el.focus(); return; }
                }
            }
            """)
            await asyncio.sleep(0.2)

            # Inject file via DataTransfer + paste event
            result = await self._page.evaluate(f"""
            async () => {{
                try {{
                    // 1. Decode base64 → Uint8Array
                    const b64 = '{b64_data}';
                    const bin = atob(b64);
                    const arr = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);

                    // 2. Buat File object
                    const blob = new Blob([arr], {{ type: '{mime}' }});
                    const file = new File([blob], '{name}', {{ type: '{mime}' }});

                    // 3. Coba tulis ke clipboard API (butuh permission, mungkin gagal)
                    try {{
                        const item = new ClipboardItem({{ '{mime}': blob }});
                        await navigator.clipboard.write([item]);
                    }} catch (_) {{
                        // Clipboard API tidak tersedia / blocked — lanjut dengan paste event
                    }}

                    // 4. Buat DataTransfer dengan file
                    const dt = new DataTransfer();
                    dt.items.add(file);

                    // 5. Cari target element (textarea atau contenteditable)
                    const selectors = [
                        'textarea[placeholder]',
                        'textarea',
                        'div[contenteditable="true"]',
                        '.chat-input',
                        '[class*="input-area"]',
                    ];
                    let target = null;
                    for (const sel of selectors) {{
                        const el = document.querySelector(sel);
                        if (el) {{ target = el; break; }}
                    }}
                    if (!target) return {{ ok: false, reason: 'input not found' }};

                    target.focus();

                    // 6. Dispatch paste event SAJA dulu.
                    //    Drop event hanya akan dicoba dari Python jika paste
                    //    tidak menghasilkan preview — dispatch keduanya sekaligus
                    //    menyebabkan file terdaftar 2x di Qwen.
                    const pasteEvent = new ClipboardEvent('paste', {{
                        bubbles: true,
                        cancelable: true,
                        clipboardData: dt,
                    }});
                    const accepted = target.dispatchEvent(pasteEvent);

                    return {{ ok: true, fileName: '{name}', mime: '{mime}', accepted }};

                }} catch (e) {{
                    return {{ ok: false, reason: e.toString() }};
                }}
            }}
            """)

            if not result or not result.get("ok"):
                reason = result.get("reason", "unknown") if result else "null result"
                self.logger.debug("CDP paste JS error untuk '%s': %s", att.name, reason)
                return False

            self.logger.info(
                "✅ Clipboard paste berhasil: '%s' (%s)", att.name, mime,
            )

            # Cek preview dulu dari paste event
            preview_ok = await self._wait_attachment_preview(timeout=5.0)

            # Jika paste tidak menghasilkan preview, coba drop event sebagai fallback
            if not preview_ok:
                self.logger.debug(
                    "Paste event tidak menghasilkan preview untuk '%s' — coba drop event",
                    att.name,
                )
                dropped = await self._page.evaluate(f"""
                async () => {{
                    try {{
                        const b64 = '{b64_data}';
                        const bin = atob(b64);
                        const arr = new Uint8Array(bin.length);
                        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                        const blob = new Blob([arr], {{ type: '{mime}' }});
                        const file = new File([blob], '{name}', {{ type: '{mime}' }});
                        const dt = new DataTransfer();
                        dt.items.add(file);
                        const selectors = [
                            'textarea[placeholder]', 'textarea',
                            'div[contenteditable="true"]', '.chat-input',
                            '[class*="input-area"]',
                        ];
                        let target = null;
                        for (const sel of selectors) {{
                            const el = document.querySelector(sel);
                            if (el) {{ target = el; break; }}
                        }}
                        if (!target) return false;
                        target.focus();
                        const dropEvent = new DragEvent('drop', {{
                            bubbles: true, cancelable: true, dataTransfer: dt,
                        }});
                        target.dispatchEvent(dropEvent);
                        return true;
                    }} catch (e) {{ return false; }}
                }}
                """)
                if dropped:
                    preview_ok = await self._wait_attachment_preview(timeout=5.0)

            if not preview_ok:
                self.logger.warning(
                    "Preview '%s' tidak terdeteksi — file mungkin tidak diterima Qwen",
                    att.name,
                )
            return preview_ok

        except Exception as e:
            self.logger.error(
                "CDP paste exception untuk '%s': %s", att.name, e, exc_info=True,
            )
            return False

    async def _wait_attachment_preview(self, timeout: float = 10.0) -> bool:
        """
        Tunggu sampai elemen preview/thumbnail attachment muncul di UI Qwen.
        Return True jika preview terdeteksi, False jika timeout.

        Qwen menampilkan preview berupa:
        - Thumbnail gambar (img) di area input
        - Badge/chip dengan nama file
        - Indikator upload (spinner yang kemudian berubah jadi preview)
        """
        _PREVIEW_SELECTORS = [
            # Selector spesifik Qwen (update jika class berubah)
            "[class*='attachment']",
            "[class*='file-preview']",
            "[class*='upload-preview']",
            "[class*='file-card']",
            "[class*='file-chip']",
            "[class*='attached']",
            # Gambar thumbnail di area input
            ".chat-input-area img:not([class*='avatar'])",
            ".input-area img:not([class*='avatar'])",
            # Generic fallback
            "[class*='preview-item']",
            "[class*='upload-item']",
        ]

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for sel in _PREVIEW_SELECTORS:
                try:
                    els = await self._page.query_selector_all(sel)
                    for el in els:
                        if await el.is_visible():
                            self.logger.debug(
                                "Attachment preview terdeteksi via '%s'", sel,
                            )
                            return True
                except Exception:
                    continue
            await asyncio.sleep(0.3)

        self.logger.debug("Attachment preview timeout setelah %.0fs", timeout)
        return False

    # ── Create Image / Create Video ───────────────────────────────────────────

    # Selector untuk tombol "Create Image" dan "Create Video" di toolbar Qwen
    _SEL_CREATE_IMAGE_BTN = [
        "button[aria-label*='Create Image' i]",
        "button[aria-label*='image' i][class*='create' i]",
        "[class*='create-image']",
        "[data-testid*='image-gen']",
        "button[title*='Create Image' i]",
        # Fallback: cari teks label di dalam button
        "button",   # difilter via JS innerText di bawah
    ]

    _SEL_CREATE_VIDEO_BTN = [
        "button[aria-label*='Create Video' i]",
        "button[aria-label*='video' i][class*='create' i]",
        "[class*='create-video']",
        "[data-testid*='video-gen']",
        "button[title*='Create Video' i]",
        "button",   # difilter via JS innerText di bawah
    ]

    _SEL_WEB_SEARCH_BTN = [
        "button[aria-label*='Web search' i]",
        "button[aria-label*='Search' i][class*='web' i]",
        "[class*='web-search']",
        "[data-testid*='web-search']",
        "button[title*='Web search' i]",
        "button",   # fallback scan innerText
    ]

    async def _open_toolbar_menu(self) -> bool:
        """
        Klik elemen bertanda 'mode-select-open' untuk memunculkan dropdown
        Ant Design yang berisi 'Create image', 'Create Video', 'Web search', dll.
        """
        selectors = [
            # Selector utama — class yang diketahui dari DOM Qwen
            ".mode-select-open",
            "[class*='mode-select-open']",
            "button.mode-select-open",
            "span.mode-select-open",
            "div.mode-select-open",
            # Fallback tambahan
            "button[class*='mode-select']",
            "[class*='mode-select-btn']",
            "[class*='mode-select-trigger']",
        ]

        for sel in selectors:
            try:
                els = await self._page.query_selector_all(sel)
                for el in els:
                    if await el.is_visible():
                        await el.click()
                        self.logger.info("✅ Toolbar menu dibuka via '%s'", sel)
                        await asyncio.sleep(0.6)
                        return True
            except Exception:
                continue

        # Fallback JS — cari elemen apapun yang class-nya mengandung 'mode-select-open'
        opened = await self._page.evaluate("""
        () => {
            const el = document.querySelector(
                '[class*="mode-select-open"], [class*="mode-select-btn"], [class*="mode-select-trigger"]'
            );
            if (el) { el.click(); return true; }
            return false;
        }
        """)
        if opened:
            self.logger.info("✅ Toolbar menu dibuka via JS querySelector")
            await asyncio.sleep(0.6)
            return True

        self.logger.warning("⚠️  Tombol toolbar 'mode-select-open' tidak ditemukan")
        return False

    async def _find_and_click_menu_item(self, keyword: str) -> bool:
        """
        Cari dan klik item di dropdown Ant Design berdasarkan keyword.

        Struktur DOM Qwen:
          <li class="ant-dropdown-menu-item ant-dropdown-menu-item-only-child mode-select-common-item">
            <span class="ant-dropdown-menu-title-content">
              <span class="mode-select-dropdown-item">Create image</span>
            </span>
          </li>

        Berlaku untuk: 'Create image', 'Create Video', 'Web search', dll.
        """
        keyword_low = keyword.lower()

        # ── Selector spesifik Ant Design Qwen ─────────────────────────────────
        ant_selectors = [
            # Exact match via span dalam menu item
            ".ant-dropdown-menu-item .mode-select-dropdown-item",
            ".ant-dropdown-menu-item span.ant-dropdown-menu-title-content",
            ".ant-dropdown-menu-item",
            # Generic dropdown
            "[class*='ant-dropdown'] li",
            "[class*='ant-dropdown'] [class*='menu-item']",
            "[class*='mode-select-dropdown-item']",
            "[class*='mode-select-common-item']",
        ]

        for sel in ant_selectors:
            try:
                els = await self._page.query_selector_all(sel)
                for el in els:
                    if not await el.is_visible():
                        continue
                    text = (await el.inner_text() or "").lower().strip()
                    if keyword_low in text:
                        await el.click()
                        self.logger.info(
                            "✅ Menu item '%s' diklik via selector '%s'", keyword, sel
                        )
                        await asyncio.sleep(0.5)
                        return True
            except Exception:
                continue

        # ── Fallback JS scan seluruh DOM ──────────────────────────────────────
        found = await self._page.evaluate(f"""
        () => {{
            const kw = '{keyword_low}';
            // Prioritaskan elemen Ant Design
            const priority = Array.from(document.querySelectorAll(
                '.ant-dropdown-menu-item, [class*="mode-select"], [class*="dropdown-item"]'
            ));
            for (const el of priority) {{
                if (el.offsetParent === null) continue;
                const txt = (el.innerText || el.textContent || '').toLowerCase().trim();
                if (txt.includes(kw)) {{ el.click(); return true; }}
            }}
            // Fallback: semua elemen interaktif
            const all = Array.from(document.querySelectorAll(
                'li, button, [role="menuitem"], [role="option"], a, span'
            ));
            for (const el of all) {{
                if (el.offsetParent === null) continue;
                const txt = (
                    el.innerText || el.textContent ||
                    el.getAttribute('aria-label') || ''
                ).toLowerCase().trim();
                if (txt === kw || (txt.includes(kw) && txt.length < kw.length + 10)) {{
                    el.click();
                    return true;
                }}
            }}
            return false;
        }}
        """)

        if found:
            self.logger.info("✅ Menu item '%s' diklik via JS scan", keyword)
            await asyncio.sleep(0.5)
            return True

        return False

        """
        Aktifkan mode Web Search di Qwen.

        Alur: buka toolbar menu '+' → klik item 'Web search'.
        Jika item sudah visible di halaman (tanpa buka menu), langsung klik.
        """
        keyword = "Web search"
        self.logger.info("Mengaktifkan mode '%s' …", keyword)

        # Coba langsung dulu (kalau sudah visible tanpa buka menu)
        if await self._find_and_click_menu_item(keyword):
            return True

        # Buka toolbar menu dulu, lalu cari lagi
        await self._open_toolbar_menu()
        if await self._find_and_click_menu_item(keyword):
            return True

        # Last resort: coba klik "More" / "..." jika ada, lalu cari lagi
        await self._find_and_click_menu_item("More")
        await asyncio.sleep(0.4)
        if await self._find_and_click_menu_item(keyword):
            return True

        self.logger.warning("⚠️  Tombol '%s' tidak ditemukan", keyword)
        return False

    async def _click_web_search_button(self) -> bool:
        """
        Aktifkan mode Web Search di Qwen.
        Alur: buka toolbar menu (mode-select-open) → klik item 'Web search'.
        """
        keyword = "Web search"
        self.logger.info("Mengaktifkan mode '%s' …", keyword)

        if await self._find_and_click_menu_item(keyword):
            return True

        await self._open_toolbar_menu()
        if await self._find_and_click_menu_item(keyword):
            return True

        await self._find_and_click_menu_item("More")
        await asyncio.sleep(0.4)
        if await self._find_and_click_menu_item(keyword):
            return True

        self.logger.warning("⚠️  Tombol '%s' tidak ditemukan", keyword)
        return False

    async def web_search(
        self,
        prompt: str,
        timeout: float = 90.0,
    ) -> dict:
        """
        Kirim pertanyaan ke Qwen AI dengan mode Web Search aktif.

        Return dict:
            {
                "success"  : bool,
                "prompt"   : str,
                "response" : str,
                "error"    : str | None,
            }
        """
        self.logger.info("=== web_search: '%s'", prompt[:80])

        try:
            await self._goto_new_chat()

            clicked = await self._click_web_search_button()
            if not clicked:
                return {
                    "success" : False,
                    "prompt"  : prompt,
                    "response": "",
                    "error"   : "Tombol 'Web search' tidak ditemukan di halaman Qwen",
                }

            response_text = await self._submit_prompt(prompt)

            self.logger.info("✅ web_search selesai: %d chars", len(response_text))
            return {
                "success" : True,
                "prompt"  : prompt,
                "response": response_text,
                "error"   : None,
            }

        except Exception as e:
            self.logger.error("web_search error: %s", e, exc_info=True)
            return {
                "success" : False,
                "prompt"  : prompt,
                "response": "",
                "error"   : str(e),
            }

    async def _click_create_button(self, mode: str) -> bool:
        """
        Klik tombol 'Create Image' atau 'Create Video' di toolbar Qwen.

        mode: 'image' | 'video'

        Alur: buka toolbar menu '+' → klik item 'Create Image'/'Create Video'.
        Jika item sudah visible di halaman tanpa buka menu, langsung klik.
        """
        keyword = "Create image" if mode == "image" else "Create Video"
        self.logger.info("Mengaktifkan mode '%s' …", keyword)

        # Coba langsung dulu (kalau sudah visible tanpa buka menu)
        if await self._find_and_click_menu_item(keyword):
            return True

        # Buka toolbar menu '+' dulu, lalu cari lagi
        await self._open_toolbar_menu()
        if await self._find_and_click_menu_item(keyword):
            return True

        # Last resort: coba klik "More" / "..." jika ada, lalu cari lagi
        await self._find_and_click_menu_item("More")
        await asyncio.sleep(0.4)
        if await self._find_and_click_menu_item(keyword):
            return True

        self.logger.warning("⚠️  Tombol '%s' tidak ditemukan", keyword)
        return False

    async def _wait_media_output(
        self,
        mode: str,
        timeout: float = 120.0,
    ) -> list[str]:
        """
        Tunggu sampai Qwen selesai generate gambar/video dan ekstrak URL-nya.

        mode    : 'image' | 'video'
        timeout : detik maksimum menunggu (default 120s, generate media lebih lambat)

        Return  : list URL (bisa kosong jika tidak terdeteksi dalam timeout)
        """
        # Selector gambar/video hasil generate Qwen
        # Struktur DOM: .qwen-chat-response-control-card > .qwen-image > img/video
        if mode == "image":
            media_selectors = [
                ".qwen-chat-response-control-card img[src]",
                ".qwen-image img[src]",
                ".qwen-chat-response-control-card .qwen-image img[src]",
            ]
        else:
            media_selectors = [
                ".qwen-chat-response-control-card video[src]",
                ".qwen-image video[src]",
                ".qwen-chat-response-control-card video source[src]",
                ".qwen-chat-response-control-card .qwen-image video",
            ]

        deadline = asyncio.get_event_loop().time() + timeout
        last_log = asyncio.get_event_loop().time()

        # Tunggu sebentar agar DOM settle setelah Qwen selesai generate
        await asyncio.sleep(2.0)

        while asyncio.get_event_loop().time() < deadline:
            # Log progress setiap 15 detik
            now = asyncio.get_event_loop().time()
            if now - last_log >= 15:
                elapsed = now - (deadline - timeout)
                self.logger.info(
                    "Menunggu output %s … (%.0f/%.0fs)", mode, elapsed, timeout,
                )
                last_log = now

            # ── Metode 1: selector CSS per elemen ─────────────────────────────
            urls: list[str] = []
            for sel in media_selectors:
                try:
                    els = await self._page.query_selector_all(sel)
                    for el in els:
                        src = await el.get_attribute("src")
                        if (
                            src
                            and src.startswith("http")
                            and src not in urls
                            and "assets.alicdn.com" not in src
                            and "image-generating-icon" not in src
                        ):
                            urls.append(src)
                except Exception:
                    continue

            # ── Metode 2: JS scan via class Qwen yang diketahui ──────────────
            if not urls:
                try:
                    if mode == "image":
                        js_urls = await self._page.evaluate("""
                        () => {
                            const imgs = Array.from(document.querySelectorAll(
                                '.qwen-chat-response-control-card img[src], .qwen-image img[src]'
                            ));
                            return imgs.map(i => i.src).filter(s =>
                                s && s.startsWith('http')
                                && !s.includes('assets.alicdn.com')
                                && !s.includes('image-generating-icon')
                            );
                        }
                        """)
                    else:
                        js_urls = await self._page.evaluate("""
                        () => {
                            const vids = Array.from(document.querySelectorAll(
                                '.qwen-chat-response-control-card video[src], ' +
                                '.qwen-image video[src], ' +
                                '.qwen-chat-response-control-card source[src]'
                            ));
                            return vids
                                .map(v => v.src || v.getAttribute('src'))
                                .filter(s => s && s.startsWith('http'));
                        }
                        """)
                    for u in (js_urls or []):
                        if u not in urls:
                            urls.append(u)
                except Exception as e:
                    self.logger.debug("JS scan error: %s", e)

            if urls:
                self.logger.info(
                    "✅ %d URL %s terdeteksi: %s",
                    len(urls), mode, urls[:3],
                )
                return urls

            await asyncio.sleep(1.0)

        self.logger.warning(
            "⏱  Timeout %.0fs: URL %s tidak terdeteksi", timeout, mode,
        )
        return []

    async def create_image(
        self,
        prompt: str,
        timeout: float = 120.0,
    ) -> dict:
        """
        Generate gambar di Qwen AI via tombol 'Create Image'.

        1. Navigasi ke halaman baru.
        2. Klik tombol 'Create Image'.
        3. Ketik prompt dan submit.
        4. Tunggu gambar muncul + ekstrak URL.

        Return dict:
            {
                "success"  : bool,
                "prompt"   : str,
                "urls"     : list[str],   # URL gambar hasil generate
                "response" : str,         # teks response Qwen (jika ada)
                "error"    : str | None,
            }
        """
        self.logger.info("=== create_image: '%s'", prompt[:80])

        try:
            await self._goto_new_chat()

            clicked = await self._click_create_button("image")
            if not clicked:
                return {
                    "success": False,
                    "prompt": prompt,
                    "urls": [],
                    "response": "",
                    "error": "Tombol 'Create Image' tidak ditemukan di halaman Qwen",
                }

            # Kirim prompt langsung — mode sudah diset via _click_create_button
            response_text = await self._submit_prompt_media(prompt, "image")

            # Ekstrak URL gambar dari DOM
            urls = await self._wait_media_output("image", timeout=timeout)

            # Fallback: coba ekstrak URL dari teks response (kadang Qwen menyertakan link)
            if not urls:
                import re
                found_urls = re.findall(r'https?://\S+\.(?:jpg|jpeg|png|webp|gif)', response_text, re.I)
                urls = list(dict.fromkeys(found_urls))   # dedup, preserve order

            return {
                "success": True,
                "prompt": prompt,
                "urls": urls,
                "response": response_text,
                "error": None,
            }

        except Exception as e:
            self.logger.error("create_image error: %s", e, exc_info=True)
            return {
                "success": False,
                "prompt": prompt,
                "urls": [],
                "response": "",
                "error": str(e),
            }

    async def create_video(
        self,
        prompt: str,
        timeout: float = 180.0,
    ) -> dict:
        """
        Generate video di Qwen AI via tombol 'Create Video'.

        Sama dengan create_image namun untuk video.
        Timeout default lebih panjang (180s) karena render video lebih lambat.

        Return dict:
            {
                "success"  : bool,
                "prompt"   : str,
                "urls"     : list[str],   # URL video hasil generate
                "response" : str,
                "error"    : str | None,
            }
        """
        self.logger.info("=== create_video: '%s'", prompt[:80])

        try:
            await self._goto_new_chat()

            clicked = await self._click_create_button("video")
            if not clicked:
                return {
                    "success": False,
                    "prompt": prompt,
                    "urls": [],
                    "response": "",
                    "error": "Tombol 'Create Video' tidak ditemukan di halaman Qwen",
                }

            # Kirim prompt langsung — mode sudah diset via _click_create_button
            response_text = await self._submit_prompt_media(prompt, "video")

            urls = await self._wait_media_output("video", timeout=timeout)

            # Fallback: cari URL video dari teks response
            if not urls:
                import re
                found_urls = re.findall(r'https?://\S+\.(?:mp4|webm|mov|avi)', response_text, re.I)
                urls = list(dict.fromkeys(found_urls))

            return {
                "success": True,
                "prompt": prompt,
                "urls": urls,
                "response": response_text,
                "error": None,
            }

        except Exception as e:
            self.logger.error("create_video error: %s", e, exc_info=True)
            return {
                "success": False,
                "prompt": prompt,
                "urls": [],
                "response": "",
                "error": str(e),
            }

    async def _count_media_elements(self) -> int:
        """
        Hitung elemen media HASIL JADI generate di DOM.
        Abaikan placeholder/loading icon (assets.alicdn.com, image-generating-icon).

        Struktur DOM Qwen untuk hasil generate:
          .qwen-chat-response-control-card   ← container card hasil
            .qwen-image                      ← wrapper gambar
              img[src]                       ← gambar hasil
            video[src]                       ← video hasil (jika ada)
        """
        return await self._page.evaluate("""
        () => {
            function isPlaceholder(src) {
                return !src
                    || src.includes('assets.alicdn.com')
                    || src.includes('image-generating-icon');
            }

            // Hitung img yang src-nya bukan placeholder
            const imgs = Array.from(document.querySelectorAll(
                '.qwen-chat-response-control-card img[src], .qwen-image img[src]'
            )).filter(i => !isPlaceholder(i.src));

            // Hitung video elements
            const vids = document.querySelectorAll(
                '.qwen-chat-response-control-card video, .qwen-image video'
            );

            return imgs.length + vids.length;
        }
        """)

    async def _wait_for_generation_media(self, mode: str) -> str:
        """
        Poll sampai Qwen selesai generate image/video:
        - is_generating() False  AND
        - ada elemen .qwen-chat-response-control-card / .qwen-image di DOM

        Tidak ada log progress — selesai langsung return.
        """
        while True:
            generating  = await self._is_generating()
            media_count = await self._count_media_elements()

            if not generating and media_count > 0:
                self.logger.info("✅ %s generation done (%d card(s))", mode, media_count)
                return await self._extract_last_response()

            await asyncio.sleep(1.0)


    async def _submit_prompt(self, prompt: str) -> str:
        """
        Kirim prompt dan tunggu response — TANPA navigasi, TANPA think-mode setup,
        TANPA attachment. Dipakai oleh web_search yang output-nya teks biasa.
        """
        input_el  = await self._find_input()
        pre_count = await self._count_response_elements()

        self.logger.info(
            "Submitting prompt (%d chars) [pre_count=%d]", len(prompt), pre_count,
        )

        await input_el.click()
        await input_el.type("~", delay=1)
        await input_el.fill(prompt, timeout=self._fill_timeout(prompt))
        #await asyncio.sleep(0.3)

        await self._click_send_button(input_el)
        self.logger.debug("Send submitted (_submit_prompt_simple)")

        self._conversation_started = True
        return await self._wait_for_generation(pre_count)

    async def _submit_prompt_media(self, prompt: str, mode: str) -> str:
        """
        Kirim prompt dan tunggu output media (image/video) selesai di-generate.
        Pakai _wait_for_generation_media yang deteksi elemen gambar/video di DOM.
        """
        input_el = await self._find_input()

        self.logger.info(
            "Submitting %s prompt (%d chars)", mode, len(prompt),
        )

        await input_el.click()
        await input_el.type("~", delay=1)
        await input_el.fill(prompt, timeout=self._fill_timeout(prompt))
        await asyncio.sleep(0.3)

        await self._click_send_button(input_el)
        self.logger.debug("Send submitted (_submit_prompt_media)")

        self._conversation_started = True
        return await self._wait_for_generation_media(mode)

        # ── Core send_prompt ──────────────────────────────────────────────────────

    # ── Prompt wrapper [SYSTEM CONTEXT] / [USER REQUEST] ────────────────────────
    def _build_wrapped_prompt(self, prompt: str) -> str:
        """
        Bungkus prompt user ke format [SYSTEM CONTEXT] / [USER REQUEST].

        Mode tanpa tools (chat biasa):
            [SYSTEM CONTEXT]
            You are operating in API mode. Respond ONLY in JSON format as specified.

            [USER REQUEST]
            {"prompt": "...", "model": "account1"}

        Mode dengan tools (LLM API + tool calling):
            [SYSTEM CONTEXT]
            You are a strict JSON LLM API endpoint.

            Available tools:
            [{"type":"function","function":{"name":"write_file",...}}, ...]

            RESPONSE FORMAT RULES:
            Rule 1 — Jika perlu memanggil tool:
            {"status":"tool_calls","tool_calls":[{"id":"call_<id>","type":"function",
             "function":{"name":"<name>","arguments":{...}}}]}

            Rule 2 — Jika sudah punya jawaban final:
            {"status":"success","choices":[{"index":0,"message":{"role":"assistant",
             "content":"<jawaban>"},"finish_reason":"stop"}]}

            [USER REQUEST]
            {"prompt": "...", "model": "account1", "max_tokens": 2000}
        """
        account_name = (
            self._current_cookie_file.stem if self._current_cookie_file else "qwen"
        )
        user_request: dict = {"prompt": prompt, "model": account_name}
        if self._max_tokens is not None:
            user_request["max_tokens"] = self._max_tokens

        parts = ["[SYSTEM CONTEXT]"]

        if self._tools:
            # ── Mode: LLM API dengan tool calling ────────────────────────────
            # PENTING: hindari kata "tool" di instruksi karena men-trigger
            # mekanisme tool calling internal Qwen UI → gunakan "function/command".
            tool_names = [
                t.get("function", {}).get("name", "")
                for t in self._tools
                if t.get("function", {}).get("name")
            ]
            parts += [
                "You are a pure JSON API endpoint. You do NOT have any built-in",
                "capabilities (no web search, no code execution, no file access,",
                "no image generation). You are a stateless text-in/JSON-out processor.",
                "",
                "The client system has its own external executor that can run the",
                "following named functions on your behalf:",
                "",
                "AVAILABLE FUNCTIONS (client-side, executed externally):",
                json.dumps(self._tools, ensure_ascii=False, indent=2),
                "",
                f"These are the ONLY function names you may request: {tool_names}",
                "Do NOT invent or use any other function name.",
                "",
                "RESPONSE FORMAT — choose exactly ONE:",
                "",
                "A) If you need the client to execute a function before you can answer:",
                '{"status":"tool_calls","tool_calls":[{"id":"call_<unique_id>","type":"function","function":{"name":"<function_name>","arguments":{<args_as_object>}}}]}',
                "",
                "B) If you have enough information to give a final answer:",
                '{"status":"success","choices":[{"index":0,"message":{"role":"assistant","content":"<jawaban_lengkap>"},"finish_reason":"stop"}]}',
                "",
                "RULES:",
                "- Output ONLY raw JSON. No markdown, no explanation, no extra text.",
                "- arguments MUST be a JSON object (dict), NOT a string.",
                "- id MUST be unique per call, format: call_<number_or_letters>.",
                "- Do NOT add any field outside the schemas above.",
                f"- Only use function names from this list: {tool_names}",
            ]
        else:
            # ── Mode: chat biasa tanpa tool calling ──────────────────────────
            parts.append(
                "You are operating in API mode. Respond ONLY in JSON format as specified."
            )

        parts += ["", "[USER REQUEST]", json.dumps(user_request, ensure_ascii=False)]
        return "\n".join(parts)

    def _build_tool_result_prompt(
        self,
        tool_messages: list[dict],
        next_user_msg: str | None = None,
    ) -> str:
        """
        Bangun prompt untuk Turn 2 (inject tool result ke conversation Qwen yang sama).

        Format yang dikirim ke Qwen (CONTINUE mode):
            [TOOL RESULT]
            {"tool_call_id":"call_001","name":"write_file","result":{"success":true}}

            [USER REQUEST]
            {"continue":true,"model":"account1"}

        Jika ada next_user_msg (user kirim pesan baru setelah tool result):
            [USER REQUEST]
            {"prompt":"sekarang jalankan","model":"account1"}
        """
        account_name = (
            self._current_cookie_file.stem if self._current_cookie_file else "qwen"
        )
        parts = ["[TOOL RESULT]"]

        for tm in tool_messages:
            entry = {
                "tool_call_id": tm.get("tool_call_id"),
                "name": tm.get("name"),
                "result": tm.get("content"),
            }
            parts.append(json.dumps(entry, ensure_ascii=False))

        user_request: dict = {"model": account_name}
        if next_user_msg:
            user_request["prompt"] = next_user_msg
        else:
            user_request["continue"] = True   # sinyal: tidak ada user message baru
        if self._max_tokens is not None:
            user_request["max_tokens"] = self._max_tokens

        parts += ["", "[USER REQUEST]", json.dumps(user_request, ensure_ascii=False)]
        return "\n".join(parts)

    async def scrape_with_tool_result(
        self,
        tool_messages: list[dict],
        next_user_msg: str | None = None,
    ) -> dict:
        """
        Kirim tool result ke Qwen dalam CONTINUE session (Turn 2).
        Dipanggil oleh newpublic_BETA.py setelah CLI mengeksekusi tool via MCP.

        Args:
            tool_messages: list of {role:tool, tool_call_id, name, content} dicts
            next_user_msg: pesan user berikutnya (opsional), jika ada setelah tool result

        Returns:
            dict result dari base_scraper.scrape() dengan finish_reason dan
            kemungkinan tool_calls (jika Qwen minta tool lagi) atau response (stop).
        """
        prompt = self._build_tool_result_prompt(tool_messages, next_user_msg)
        # wrap_as_user_request=False karena prompt sudah berformat lengkap
        # ([TOOL RESULT] / [USER REQUEST]) — tidak perlu dibungkus lagi.
        # mode="continue" wajib: tool result harus dikirim ke session yang sama,
        # bukan membuka chat baru.
        response_text = await self.send_prompt(
            prompt, mode="continue", wrap_as_user_request=False
        )
        # send_prompt mengembalikan str; bungkus ke dict agar caller bisa .get()
        from scrapers.base_qwen import BaseAIChatScraper
        is_valid, parsed, err = BaseAIChatScraper._validate_qwen_response(response_text)
        if is_valid and parsed:
            status = parsed.get("status")
            if status == "tool_calls":
                return {
                    "success": True,
                    "finish_reason": "tool_calls",
                    "tool_calls": parsed.get("tool_calls", []),
                }
            content = parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"success": True, "finish_reason": "stop", "response": content}
        return {"success": False, "error": f"Invalid response after tool result: {err}", "raw": response_text}

    async def send_prompt(
        self,
        prompt: str,
        mode: str = "new",
        think_mode: ThinkMode | None = None,
        attachments: list[Attachment] | None = None,
        wrap_as_user_request: bool = True,
    ) -> str:
        await self._ensure_page_ready(mode)
        self._last_prompt = prompt

        # Bungkus prompt asli user dengan [SYSTEM CONTEXT]/[USER REQUEST].
        # Corrective retry feedback (dari base_scraper._validate_qwen_response
        # retry loop) dikirim dengan wrap_as_user_request=False karena pesan
        # itu sendiri sudah berupa instruksi sistem, bukan request user baru.
        outgoing_prompt = self._build_wrapped_prompt(prompt) if wrap_as_user_request else prompt

        effective_think = think_mode or self._think_mode

        # Apply think mode whenever: new page loaded, explicit per-call override,
        # or not yet applied in this session.
        if not self._think_mode_applied or think_mode is not None:
            success = await self._set_think_mode(effective_think)
            if not success:
                # Run diagnostic scan so the user can identify correct selectors
                await self.debug_think_mode_selectors()

        # ── Upload attachments SEBELUM mengisi prompt ─────────────────────────
        # Urutan ini penting: Qwen membutuhkan file terupload lebih dulu
        # sebelum prompt diketik agar keduanya terkirim dalam satu request.
        if attachments:
            self.logger.info(
                "Uploading %d attachment(s): %s",
                len(attachments), [a.name for a in attachments],
            )
            await self._upload_attachments(attachments)
            # Jeda singkat agar Qwen memproses upload sebelum prompt diketik
            await asyncio.sleep(0.5)

        input_el = await self._find_input()

        # Snapshot jumlah response SEBELUM mengisi prompt dan submit,
        # supaya tidak ada race condition antara fill() dan pre_count.
        pre_count = await self._count_response_elements()

        self.logger.info(
            "Submitting prompt (%d chars, wrapped=%d chars) [think_mode=%s, pre_count=%d, attachments=%d]",
            len(prompt), len(outgoing_prompt), effective_think, pre_count,
            len(attachments) if attachments else 0,
        )

        await input_el.click()
        #await input_el.fill()
        await input_el.type("~", delay=1)  # focus and clear existing content
        await input_el.fill(outgoing_prompt, timeout=self._fill_timeout(outgoing_prompt))
        await asyncio.sleep(0.3)

        # Klik send button dengan retry+re-query untuk handle DOM detach
        await self._click_send_button(input_el)
        self.logger.debug("Send submitted (send_prompt main)")

        self._conversation_started = True
        return await self._wait_for_generation(pre_count)

    async def _count_response_elements(self) -> int:
        """Hitung jumlah elemen response saat ini di DOM."""
        return await self._page.evaluate("""
        () => document.querySelectorAll(
            '.chat-message-container .chat-response-message, .qwen-markdown-loose, .qwen-markdown'
        ).length
        """)

    async def _wait_for_generation(self, pre_count: int = 0) -> str:
        timeout_s = _TIMEOUT["response_wait"] // 1000
        stability_interval = _TIMEOUT["stability_check"] / 1000
        stability_needed = 0.5

        deadline = asyncio.get_event_loop().time() + timeout_s
        prev_text = ""
        stable_count = 0
        appeared = False

        # Fase 1: tunggu sinyal bahwa generasi sudah dimulai.
        # Dua kondisi yang dianggap "dimulai":
        #   (a) is_generating() → True  (stop-button / streaming indicator muncul)
        #   (b) cur_count > pre_count   (elemen response baru muncul di DOM)
        # Jika dalam 10 detik tidak ada sinyal, log warning dan lanjut saja
        # agar tidak stuck selamanya hanya di fase deteksi awal.
        signal_deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < signal_deadline:
            generating = await self._is_generating()
            cur_count = await self._count_response_elements()
            if generating or cur_count > pre_count:
                appeared = True
                self.logger.debug(
                    "Generation signal detected: generating=%s, count %d→%d",
                    generating, pre_count, cur_count,
                )
                break
            await asyncio.sleep(0.4)

        if not appeared:
            self.logger.warning(
                "No generation signal after 10s (pre_count=%d) – proceeding to wait anyway",
                pre_count,
            )

        # Fase 2: tunggu konten stabil
        while asyncio.get_event_loop().time() < deadline:
            generating = await self._is_generating()
            current_text = await self._extract_last_response()

            self.logger.debug(
                "generating=%s text_len=%d stable=%d/%d",
                generating, len(current_text), stable_count, stability_needed,
            )

            if current_text and not generating:
                if current_text == prev_text:
                    stable_count += 1
                    if stable_count >= stability_needed:
                        self.logger.info("Response ready (%d chars)", len(current_text))
                        return current_text
                else:
                    stable_count = 0
            else:
                stable_count = 0

            prev_text = current_text
            await asyncio.sleep(stability_interval)

        raise TimeoutError(f"Qwen did not finish responding within {timeout_s}s")

    # ── Error detection ───────────────────────────────────────────────────────

    async def is_rate_limited(self) -> bool:
        try:
            body = await self._page.inner_text("body")
            return contains_any(body, ROTATION_CONFIG["rate_limit_phrases"])
        except Exception:
            return False

    async def is_session_expired(self) -> bool:
        """Return True jika sesi tidak valid (halaman login atau tombol Login terdeteksi)."""
        try:
            url = self._page.url or ""
            # Cek URL patterns
            for pat in QWEN_AUTH_CONFIG["login_url_patterns"]:
                if pat in url.lower():
                    return True
            # Cek tombol Login/Sign Up di DOM
            if await self._is_unauthenticated():
                return True
            body = await self._page.inner_text("body")
            return contains_any(body, ROTATION_CONFIG["session_expired_phrases"])
        except Exception:
            return False

    # ── Concurrent scraping ───────────────────────────────────────────────────

    @classmethod
    async def scrape_many(
        cls,
        prompts: list[str],
        mode: str = "new",
        think_mode: ThinkMode | None = None,
        headless: bool = True,
        cookies_dir: Path | str | None = None,
        max_concurrent: int = 3,
    ) -> list[dict]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _single(prompt: str) -> dict:
            async with semaphore:
                async with cls(
                    headless=headless,
                    cookies_dir=cookies_dir,
                    think_mode=think_mode,
                ) as scraper:
                    return await scraper.scrape(prompt, mode=mode)

        tasks = [_single(p) for p in prompts]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final = []
        for p, r in zip(prompts, results):
            if isinstance(r, Exception):
                final.append({
                    "prompt": p,
                    "response": None,
                    "success": False,
                    "error": str(r),
                })
            else:
                final.append(r)
        return final