"""
BaseAIChatScraper – abstract async base class for AI chat scrapers.

Provides:
  • Browser lifecycle  (launch / close) – supports both persistent-context
                        and legacy ephemeral-context modes
  • Cookie management  (load / save / seed into profile)
  • Dynamic-content waiting with stability detection
  • Output helpers     (JSON, code files)
  • Account-rotation   framework
"""

from __future__ import annotations

import asyncio
import json
import time as _time
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
    """Hitung token via tiktoken cl100k_base. Fallback ke estimasi jika tidak tersedia."""
    if _TK_ENC is not None:
        try:
            return len(_TK_ENC.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from config import (
    BROWSER_CONFIG,
    CODE_OUTPUT_DIR,
    COOKIES_DIR,
    OUTPUT_DIR,
    OUTPUT_CONFIG,
    PERSISTENT_CONTEXT_CONFIG,
    PROFILES_DIR,
    ROTATION_CONFIG,
)
from scrapers.utils import (
    contains_any,
    detect_file_type,
    discover_cookie_files,
    extract_code_blocks,
    load_json,
    normalize_cookies,
    retry_sleep,
    safe_filename,
    save_code_files,
    save_json,
    setup_logger,
    timestamped_filename,
)


class BaseAIChatScraper(ABC):
    """
    Abstract base class for async AI-chat scrapers.

    Persistent-context mode (default, PERSISTENT_CONTEXT_CONFIG["enabled"]=True):
        • Playwright's launch_persistent_context() is used.
        • Each account maps to a dedicated profile directory under PROFILES_DIR.
        • On the very first run for an account the cookie file is seeded into
          the profile; subsequent runs reuse the stored browser state directly
          (no cookie injection needed → faster startup, cookies/localStorage
          survive across restarts).
        • self._browser is None in this mode; self._context is the persistent
          BrowserContext returned by Playwright.

    Ephemeral mode (PERSISTENT_CONTEXT_CONFIG["enabled"]=False):
        • Original Browser + BrowserContext flow; no state is persisted.
    """

    # ── Construction ──────────────────────────────────────────────────────────

    def __init__(
        self,
        headless: bool = True,
        cookies_path: Path | str | None = None,
        *,
        cookies_dir: Path | str | None = None,
    ) -> None:
        self.logger = setup_logger(self.__class__.__name__)
        self.headless = headless

        self.cookies_path: Path | None = Path(cookies_path) if cookies_path else None
        self.cookies_dir: Path = Path(cookies_dir) if cookies_dir else COOKIES_DIR
        self._cookie_files: list[Path] = []
        self._cookie_index: int = 0

        self._playwright = None
        self._browser: Browser | None = None       # None in persistent mode
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        self._persistent_mode: bool = PERSISTENT_CONTEXT_CONFIG["enabled"]

    # ── Profile path helpers ──────────────────────────────────────────────────

    def _profile_dir_for(self, cookie_file: Path | None) -> Path:
        """Return the profile directory that corresponds to *cookie_file*."""
        if cookie_file:
            return PROFILES_DIR / cookie_file.stem
        return PROFILES_DIR / PERSISTENT_CONTEXT_CONFIG["default_profile"]

    def _profile_seeded(self, profile_dir: Path) -> bool:
        """
        Return True if this profile has already been bootstrapped with cookies.
        We consider it seeded when:
          1. The Chromium 'Default' sub-dir exists (created by Playwright on first launch), AND
          2. A sentinel file 'cookies_seeded' exists (written after successful cookie injection).
        This prevents re-seeding being skipped when the profile dir exists but cookies
        were never injected (e.g. first run failed mid-way).
        """
        return (profile_dir / "Default").exists() and (profile_dir / "cookies_seeded").exists()

    # ── Browser lifecycle ─────────────────────────────────────────────────────

    async def launch_browser(self, cookie_file: Path | None = None) -> None:
        """
        Start Playwright and either:
          • Open a persistent context for *cookie_file*'s profile (default), or
          • Launch an ephemeral browser + context (legacy mode).

        *cookie_file* is used only to determine which profile directory to open;
        if None the default profile is used.
        """
        self.logger.info(
            "Launching browser (headless=%s, persistent=%s)",
            self.headless, self._persistent_mode,
        )
        self._playwright = await async_playwright().start()

        if self._persistent_mode:
            await self._launch_persistent(cookie_file)
        else:
            await self._launch_ephemeral()

        self.logger.debug("Browser launched successfully")

    async def _launch_persistent(self, cookie_file: Path | None) -> None:
        """Launch a persistent browser context, seeding cookies on first run."""
        cfg = PERSISTENT_CONTEXT_CONFIG
        profile_dir = self._profile_dir_for(cookie_file)
        profile_dir.mkdir(parents=True, exist_ok=True)

        first_run = not self._profile_seeded(profile_dir)

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.headless,
            slow_mo=BROWSER_CONFIG["slow_mo"],
            viewport=BROWSER_CONFIG["viewport"],
            user_agent=BROWSER_CONFIG["user_agent"],
            locale=BROWSER_CONFIG["locale"],
            timezone_id=BROWSER_CONFIG["timezone_id"],
            args=cfg["args"],
        )
        # Reuse existing page or open a new one
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._browser = None  # not used in persistent mode

        # Seed cookies into the profile on the very first run.
        # Navigate to the base domain first so cookies are accepted by the browser.
        if first_run and cookie_file and cookie_file.exists():
            self.logger.info("First run for profile '%s' – seeding cookies", profile_dir.name)
            await self._page.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=30_000)
            seeded = await self.load_cookies(cookie_file)
            if seeded:
                # Write sentinel so we know cookies were successfully injected
                (profile_dir / "cookies_seeded").write_text("1", encoding="utf-8")
                self.logger.info("Cookie seeding complete for profile '%s'", profile_dir.name)
            # Reload so the seeded cookies take effect
            await self._page.reload(wait_until="domcontentloaded", timeout=30_000)
        elif not first_run:
            self.logger.info("Reusing existing profile '%s'", profile_dir.name)

    async def _launch_ephemeral(self) -> None:
        """Original ephemeral browser + context launch."""
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=BROWSER_CONFIG["slow_mo"],
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )
        self._context = await self._browser.new_context(
            viewport=BROWSER_CONFIG["viewport"],
            user_agent=BROWSER_CONFIG["user_agent"],
            locale=BROWSER_CONFIG["locale"],
            timezone_id=BROWSER_CONFIG["timezone_id"],
        )
        self._page = await self._context.new_page()
        # Navigate to base domain first so cookies are accepted
        await self._page.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=30_000)

    async def close_browser(self) -> None:
        """Gracefully close the context / browser and stop Playwright."""
        self.logger.info("Closing browser")
        if self._context:
            await self._context.close()
        if self._browser:          # None in persistent mode – skip
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
    # ── Page crash detection & browser restart ────────────────────────────────

    async def _is_page_crashed(self) -> bool:
        """
        Deteksi apakah halaman Qwen mengalami crash / error fatal.

        Indikator:
          1. Halaman menampilkan frasa crash dari ROTATION_CONFIG["page_crash_phrases"].
          2. Page object tidak responsif.
        """
        try:
            if not self._page:
                return True

            try:
                body_text = await self._page.inner_text("body", timeout=5_000)
                body_lower = body_text.lower()
                for phrase in ROTATION_CONFIG.get("page_crash_phrases", []):
                    if phrase.lower() in body_lower:
                        self.logger.warning(
                            "Page crash detected: found phrase '%s'", phrase
                        )
                        return True
            except Exception:
                self.logger.warning("Could not read page body – possible crash")
                return True

            return False

        except Exception as e:
            self.logger.warning("Error during crash detection: %s", e)
            return True

    async def restart_browser(self, cookie_file: Path | None = None) -> bool:
        """
        Restart browser sepenuhnya (close + relaunch) dengan cookie yang sama.

        Dipakai saat halaman crash / error fatal yang tidak bisa di-recover
        hanya dengan navigate ulang.

        Returns True jika restart berhasil.
        """
        import time as _time
        target_cookie = cookie_file or self._current_cookie_file or self.cookies_path
        self.logger.warning(
            "🔄 Restarting browser (account: %s) …",
            target_cookie.name if target_cookie else "default",
        )

        _time.sleep(ROTATION_CONFIG.get("browser_restart_delay", 5))

        try:
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

            self._context = None
            self._browser = None
            self._page = None
            self._playwright = None

            await self.launch_browser(cookie_file=target_cookie)
            self.logger.info("✅ Browser restart selesai")
            return True

        except Exception as e:
            self.logger.error("❌ Browser restart gagal: %s", e, exc_info=True)
            return False


    # ── Cookie management ─────────────────────────────────────────────────────

    async def load_cookies(self, path: Path | str | None = None) -> bool:
        """
        Inject cookies from *path* into the current context.
        In persistent mode this is only needed once (profile seeding).
        Returns True on success.
        """
        target = Path(path) if path else self.cookies_path
        if not target:
            self.logger.warning("No cookies path provided – skipping load")
            return False
        if not target.exists():
            self.logger.warning("Cookie file not found: %s", target)
            return False

        try:
            raw = load_json(target)
            cookies = normalize_cookies(raw if isinstance(raw, list) else [])
            await self._context.add_cookies(cookies)
            self.logger.info("Loaded %d cookie(s) from %s", len(cookies), target.name)
            return True
        except Exception as exc:
            self.logger.error("Failed to load cookies from %s: %s", target, exc)
            return False

    async def save_cookies(self, path: Path | str | None = None) -> bool:
        """
        Dump current browser cookies to *path* (or self.cookies_path).
        In persistent mode the profile already persists cookies automatically;
        this method is kept for explicit export / debugging.
        Returns True on success.
        """
        target = Path(path) if path else self.cookies_path
        if not target:
            self.logger.warning("No cookies path provided – skipping save")
            return False

        try:
            cookies = await self._context.cookies()
            save_json(cookies, target)
            self.logger.info("Saved %d cookie(s) to %s", len(cookies), target.name)
            return True
        except Exception as exc:
            self.logger.error("Failed to save cookies: %s", exc)
            return False

    # ── Multi-account rotation ────────────────────────────────────────────────

    def _discover_accounts(self) -> None:
        self._cookie_files = discover_cookie_files(self.cookies_dir)
        if not self._cookie_files:
            self.logger.warning("No cookie files found in %s", self.cookies_dir)
        else:
            self.logger.info(
                "Discovered %d account cookie file(s): %s",
                len(self._cookie_files),
                [f.name for f in self._cookie_files],
            )

    @property
    def _current_cookie_file(self) -> Path | None:
        if not self._cookie_files:
            return None
        return self._cookie_files[self._cookie_index % len(self._cookie_files)]

    async def _rotate_account(self) -> bool:
        """
        Switch to the next available account.
        In persistent mode: close the current context and open the next
        account's profile directory.
        In ephemeral mode: close the current context and inject the next
        account's cookies into a fresh context.
        Returns False if all accounts are exhausted.
        """
        total = len(self._cookie_files)
        if total <= 1:
            self.logger.error("No additional accounts available for rotation")
            await self.take_debug_screenshot("no_accounts_to_rotate")
            return False

        next_index = (self._cookie_index + 1) % total
        if next_index == 0:
            self.logger.error("All accounts exhausted – no more rotation possible")
            await self.take_debug_screenshot("all_accounts_exhausted")
            return False

        self.logger.warning(
            "Rotating account: %s → %s",
            self._cookie_files[self._cookie_index].name,
            self._cookie_files[next_index].name,
        )
        self._cookie_index = next_index
        retry_sleep(ROTATION_CONFIG["rotation_delay"])

        next_cookie_file = self._cookie_files[self._cookie_index]

        if self._persistent_mode:
            # Close current persistent context and open the next profile
            if self._context:
                await self._context.close()
            await self._launch_persistent(next_cookie_file)
        else:
            # Ephemeral: recreate context and inject cookies after navigating to domain
            if self._context:
                await self._context.close()
            self._context = await self._browser.new_context(
                viewport=BROWSER_CONFIG["viewport"],
                user_agent=BROWSER_CONFIG["user_agent"],
                locale=BROWSER_CONFIG["locale"],
                timezone_id=BROWSER_CONFIG["timezone_id"],
            )
            self._page = await self._context.new_page()
            await self._page.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=30_000)
            await self.load_cookies(next_cookie_file)

        return True

    # ── Waiting helpers ───────────────────────────────────────────────────────

    async def wait_for_response(
        self,
        response_selector: str,
        loading_selector: str | None = None,
        timeout: int = 300,
        stability_checks: int = 2,
        stability_interval: float = 3.0,
    ) -> str:
        self.logger.debug("Waiting for response (timeout=%ds)", timeout)
        deadline = asyncio.get_event_loop().time() + timeout
        prev_texts: list[str] = []
        stable_count = 0

        while asyncio.get_event_loop().time() < deadline:
            if loading_selector:
                try:
                    await self._page.wait_for_selector(
                        loading_selector, state="hidden", timeout=5_000,
                    )
                except Exception:
                    pass

            try:
                elements = await self._page.query_selector_all(response_selector)
                texts = [((await el.inner_text()) or "").strip() for el in elements]
                current_text = "\n".join(texts)
            except Exception:
                current_text = ""

            if not current_text:
                await asyncio.sleep(1)
                continue

            if prev_texts and current_text == prev_texts[-1]:
                stable_count += 1
                if stable_count >= stability_checks:
                    self.logger.debug("Response stable after %d check(s)", stable_count)
                    return current_text
            else:
                stable_count = 0

            prev_texts.append(current_text)
            await asyncio.sleep(stability_interval)

        raise TimeoutError(
            f"Response not stable after {timeout}s – "
            f"last text length: {len(prev_texts[-1]) if prev_texts else 0}"
        )

    # ── Output helpers ────────────────────────────────────────────────────────

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
        self,
        blocks: list[dict],
        output_dir: Path | None = None,
        prefix: str = "snippet",
    ) -> list[Path]:
        target = output_dir or CODE_OUTPUT_DIR
        paths = save_code_files(blocks, target, prefix)
        self.logger.info("Saved %d code file(s) to %s", len(paths), target)
        return paths

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    async def send_prompt(self, prompt: str, mode: str = "new", **kwargs) -> str: ...

    @abstractmethod
    async def is_rate_limited(self) -> bool: ...

    @abstractmethod
    async def is_session_expired(self) -> bool: ...

    def _extra_send_kwargs(self) -> dict:
        """
        Subclasses can override this to inject extra keyword arguments into
        the send_prompt() call made by scrape(). For example, QwenScraper
        returns {"think_mode": self._think_mode}.
        """
        return {}

    # ── Debug screenshot ──────────────────────────────────────────────────────

    async def take_debug_screenshot(self, reason: str = "error") -> Path | None:
        """
        Ambil screenshot halaman saat ini dan simpan ke folder /debug/.

        Dipanggil otomatis pada setiap error / exhausted / timeout agar
        developer bisa melihat kondisi browser ketika masalah terjadi.

        Nama file format: debug_YYYYMMDD_HHMMSS_<reason>.png
        Return: Path file screenshot, atau None jika gagal.
        """
        try:
            if not self._page:
                self.logger.warning("Screenshot gagal: _page belum tersedia")
                return None

            debug_dir = Path("debug")
            debug_dir.mkdir(parents=True, exist_ok=True)

            # Sanitasi reason untuk nama file (ganti karakter tidak aman)
            import re as _re
            safe_reason = _re.sub(r"[^\w\-]", "_", reason)[:60]
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"debug_{ts}_{safe_reason}.png"
            path = debug_dir / filename

            await self._page.screenshot(path=str(path), full_page=True)
            self.logger.info("📸 Debug screenshot disimpan: %s", path)
            return path

        except Exception as e:
            self.logger.warning("Gagal mengambil debug screenshot: %s", e)
            return None

    # ── High-level scrape with auto-rotation ─────────────────────────────────

    # ── Response validator (poin 3) ──────────────────────────────────────────
    @staticmethod
    def _repair_unescaped_quotes(raw: str) -> "str | None":
        """
        Fallback repair untuk kasus paling umum: Qwen menulis quote literal (")
        di dalam isi `content` tanpa di-escape, sehingga merusak parsing JSON.

        Contoh kasus nyata:
          {"status":"success","choices":[{"index":0,"message":{"role":"assistant",
           "content":""Violence District" di Roblox merujuk pada..."}, ...}]}
                                          ^^                      ^^
          quote literal di awal & dalam isi content, bukan delimiter JSON.

        Strategi: cari blok `"content":"..."` dengan regex non-greedy yang
        berhenti tepat sebelum penanda akhir field yang valid (`","finish_reason"`
        atau `"}` penutup objek message), lalu escape ulang SEMUA quote dan
        backslash di dalam isi tsb sebelum di-reinsert ke string asli.

        Returns string JSON yang sudah diperbaiki, atau None jika pola
        `"content":"..."` tidak ditemukan sama sekali (repair tidak applicable).
        """
        # Cari posisi awal isi content: tepat setelah `"content":"`
        marker = '"content":"'
        start_idx = raw.find(marker)
        if start_idx == -1:
            return None
        content_start = start_idx + len(marker)

        # Cari penanda akhir field content yang valid (paling umum muncul
        # setelahnya): `","finish_reason"` atau `"}` (penutup objek message)
        # diikuti pola penutup choices. Coba beberapa kandidat, ambil yang
        # posisinya paling akhir (asumsikan isi content adalah segmen
        # terpanjang yang masuk akal antara dua penanda terluar).
        end_markers = ['","finish_reason"', '"},"finish_reason"', '"}}']
        end_idx = -1
        used_marker = ""
        for em in end_markers:
            idx = raw.rfind(em)
            if idx > content_start and (end_idx == -1 or idx > end_idx):
                end_idx = idx
                used_marker = em

        if end_idx == -1:
            return None

        inner = raw[content_start:end_idx]
        # Re-escape backslash dulu (agar tidak double-escape), lalu quote,
        # lalu normalisasi newline/tab mentah yang mungkin ikut tercopy.
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
    def _repair_tool_calls_arguments(raw: str) -> "str | None":
        """
        Repair kasus tool_calls di mana arguments berisi inner quotes yang tidak
        di-escape, sehingga menyebabkan JSON truncated/malformed.

        Contoh kasus:
          {"status":"tool_calls","tool_calls":[{"id":"call_5","type":"function",
           "function":{"name":"run_command","arguments":{"command":"powershell -Command "Get-Process...
                                                                                      ^^
           quote literal di dalam value string arguments.

        Strategi: untuk setiap string value di dalam arguments, escape semua
        inner quotes dan karakter kontrol yang tidak ter-escape.
        """
        import re
        # Temukan blok arguments: setelah `"arguments":{`
        # Cari semua string value di dalam arguments dan escape isinya
        args_marker = '"arguments":{'
        start = raw.find(args_marker)
        if start == -1:
            return None
        args_start = start + len(args_marker) - 1  # posisi `{`

        # Temukan key-value pairs di dalam arguments
        # Strategy: untuk tiap `"key":"value"`, escape inner quotes di value
        def escape_string_values(s: str) -> str:
            """Escape unescaped quotes dan newline di dalam string JSON values."""
            result = []
            i = 0
            while i < len(s):
                if s[i] == '"':
                    # Mulai string — cari penutupnya
                    result.append('"')
                    i += 1
                    while i < len(s):
                        if s[i] == '\\' and i + 1 < len(s):
                            # Already escaped — lewati dua karakter
                            result.append(s[i])
                            result.append(s[i + 1])
                            i += 2
                        elif s[i] == '"':
                            # Cek apakah ini penutup string yang valid:
                            # penutup valid diikuti `:`, `,`, `}`, `]`, atau whitespace+penutup
                            rest = s[i + 1:].lstrip()
                            if rest and rest[0] in (':', ',', '}', ']'):
                                result.append('"')
                                i += 1
                                break
                            else:
                                # Inner quote — escape
                                result.append('\\"')
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

        # Ambil sisa string dari args_start, repair, dan rebuild
        try:
            # Coba parse dulu — jika sudah valid tidak perlu repair
            import json as _json
            _json.loads(raw)
            return None  # tidak perlu repair
        except Exception:
            pass

        # Ambil segmen arguments dan coba escape inner quotes
        tail = raw[args_start:]
        repaired_tail = escape_string_values(tail)
        repaired = raw[:args_start] + repaired_tail

        # Pastikan JSON tertutup dengan benar
        try:
            import json as _json
            _json.loads(repaired)
            return repaired
        except Exception:
            return None

    @staticmethod
    def _validate_qwen_response(raw: str) -> "tuple[bool, dict | None, str]":
        """
        Validasi bahwa response Qwen adalah JSON dengan schema minimal:
          { "status": "success|error",
            "choices": [{ "index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop" }] }
        Returns: (is_valid, parsed_dict, error_reason)

        Fallback: jika json.loads() gagal karena unescaped quote di dalam
        `content` (kasus paling sering terjadi pada Qwen), dicoba sekali lagi
        dengan _repair_unescaped_quotes() sebelum dinyatakan gagal total.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # Coba repair tool_calls arguments dulu jika raw mengandung tool_calls
            repaired = None
            if '"tool_calls"' in raw or '"arguments"' in raw:
                repaired = BaseAIChatScraper._repair_tool_calls_arguments(raw)
            # Fallback ke repair unescaped quotes untuk success/error response
            if repaired is None:
                repaired = BaseAIChatScraper._repair_unescaped_quotes(raw)
            if repaired is not None:
                try:
                    data = json.loads(repaired)
                except json.JSONDecodeError as e2:
                    return False, None, (
                        f"JSON parse error: {e} | repair fallback juga gagal: {e2}"
                    )
            else:
                return False, None, f"JSON parse error: {e}"
        if not isinstance(data, dict):
            return False, None, "Response bukan JSON object"

        status = data.get("status")

        # ── Schema 1: tool_calls — Qwen meminta pemanggilan tool ─────────────
        if status == "tool_calls":
            tool_calls = data.get("tool_calls")
            if not isinstance(tool_calls, list) or len(tool_calls) == 0:
                return False, None, "tool_calls kosong atau bukan list"
            for i, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    return False, None, f"tool_calls[{i}] bukan dict"
                fn = tc.get("function", {})
                if not fn.get("name"):
                    return False, None, f"tool_calls[{i}].function.name kosong"
                args = fn.get("arguments")
                if args is not None and not isinstance(args, dict):
                    return False, None, (
                        f"tool_calls[{i}].function.arguments harus dict/object, "
                        f"bukan {type(args).__name__}. "
                        f"Qwen mungkin mengembalikan arguments sebagai JSON string."
                    )
            return True, data, ""

        # ── Schema 2: success / error — jawaban final ─────────────────────────
        if status not in ("success", "error"):
            return False, None, f"Field 'status' tidak valid: {status!r}"

        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) == 0:
            return False, None, "Field 'choices' kosong atau tidak ada"
        first = choices[0]
        msg = first.get("message", {})
        if msg.get("role") != "assistant":
            return False, None, f"choices[0].message.role bukan 'assistant': {msg.get('role')!r}"
        content_str = msg.get("content", "")
        if not isinstance(content_str, str) or not content_str.strip():
            return False, None, "choices[0].message.content kosong"
        return True, data, ""

    async def scrape(self, prompt: str, mode: str = "new", attachments: list | None = None) -> dict:
        # _discover_accounts() is already called in __aenter__ before browser launch.
        # In persistent mode, launch_browser() already handled cookie seeding.
        # In ephemeral mode, inject cookies manually after navigating to the domain.
        if not self._persistent_mode:
            initial_cookie = self._current_cookie_file or self.cookies_path
            if initial_cookie:
                await self.load_cookies(initial_cookie)

        max_total_attempts = max(len(self._cookie_files), 1) * ROTATION_CONFIG["max_retries_per_account"]
        attempt = 0
        # browser_restart_count dilacak per-akun: reset setiap kali rotate ke akun baru.
        browser_restart_count = 0
        max_browser_restarts = ROTATION_CONFIG.get("max_browser_restarts", 3)

        _rl_restart_phrases = ROTATION_CONFIG.get("rate_limit_restart_first_phrases", [])
        _rl_rotate_phrases  = ROTATION_CONFIG.get("rate_limit_rotate_phrases", [])

        # ── Retry / rotation tracking (poin 3) ───────────────────────────────
        HARD_CAP_RETRIES  = 5     # total retry lintas semua akun
        MAX_RETRY_PER_ACC = 2     # max retry per akun sebelum rotasi
        RETRY_DELAY_S     = 2.0   # jeda antar retry (detik)
        ROTATE_DELAY_S    = 5.0   # jeda saat rotasi akun (detik)

        total_retries        = 0
        retries_on_this_acc  = 0
        previous_account_err: str | None = None
        scrape_start_time    = _time.monotonic()
        # ─────────────────────────────────────────────────────────────────────

        while attempt < max_total_attempts:
            attempt += 1
            account_name = (
                self._current_cookie_file.stem if self._current_cookie_file else "default"
            )
            self.logger.info(
                "Attempt %d/%d using account '%s'",
                attempt, max_total_attempts, account_name,
            )

            try:
                # ── Cek page crash sebelum mengirim prompt ────────────────────────
                if await self._is_page_crashed():
                    self.logger.warning(
                        "⚠️  Page crash terdeteksi sebelum attempt %d – restarting browser …",
                        attempt,
                    )
                    await self.take_debug_screenshot(f"crash_before_attempt{attempt}")
                    if browser_restart_count < max_browser_restarts:
                        browser_restart_count += 1
                        restarted = await self.restart_browser()
                        if restarted:
                            self.logger.info(
                                "Browser restart #%d berhasil – melanjutkan attempt",
                                browser_restart_count,
                            )
                            attempt -= 1
                            continue
                    self.logger.error("Tidak bisa restart browser – menghentikan scrape")
                    break

                # Gabungkan kwargs dari subclass (misal think_mode) + attachments
                extra = self._extra_send_kwargs()
                if attachments:
                    extra["attachments"] = attachments
                response_text = await self.send_prompt(prompt, mode, **extra)

                # ── Rate limit: restart-first (quota/token Alibaba) ──────────────
                if contains_any(response_text, _rl_restart_phrases):
                    self.logger.warning(
                        "⚠️  Rate limit (quota/token) terdeteksi pada akun '%s' "
                        "(browser_restart=%d/%d) – mencoba restart browser dulu …",
                        account_name, browser_restart_count, max_browser_restarts,
                    )
                    await self.take_debug_screenshot(f"rate_limit_quota_attempt{attempt}")
                    if browser_restart_count < max_browser_restarts:
                        browser_restart_count += 1
                        restarted = await self.restart_browser()
                        if restarted:
                            self.logger.info(
                                "Browser restart #%d selesai – retry dengan akun yang sama",
                                browser_restart_count,
                            )
                            attempt -= 1
                            continue
                    self.logger.warning(
                        "Browser restart habis (%d/%d) – fallback rotate ke akun lain",
                        browser_restart_count, max_browser_restarts,
                    )
                    await self.take_debug_screenshot(f"rate_limit_quota_rotate_attempt{attempt}")
                    previous_account_err = f"rate_limited on {account_name}"
                    if not await self._rotate_account():
                        break
                    browser_restart_count = 0
                    retries_on_this_acc = 0
                    await asyncio.sleep(ROTATE_DELAY_S)
                    continue

                # ── Rate limit: langsung rotate akun ────────────────────────────
                if contains_any(response_text, _rl_rotate_phrases):
                    self.logger.warning("Rate limit terdeteksi (rotate-direct) – rotating account")
                    await self.take_debug_screenshot("rate_limit_rotate")
                    previous_account_err = f"rate_limited (rotate-direct) on {account_name}"
                    if not await self._rotate_account():
                        break
                    browser_restart_count = 0
                    retries_on_this_acc = 0
                    await asyncio.sleep(ROTATE_DELAY_S)
                    continue

                # ── Cek page crash di response teks ─────────────────────────────
                if contains_any(response_text, ROTATION_CONFIG.get("page_crash_phrases", [])):
                    self.logger.warning("Page crash phrase detected in response – restarting browser")
                    await self.take_debug_screenshot(f"crash_in_response_attempt{attempt}")
                    if browser_restart_count < max_browser_restarts:
                        browser_restart_count += 1
                        restarted = await self.restart_browser()
                        if restarted:
                            attempt -= 1
                            continue
                    break

                if contains_any(response_text, ROTATION_CONFIG["session_expired_phrases"]):
                    self.logger.warning("Session expired – rotating account")
                    await self.take_debug_screenshot("session_expired")
                    previous_account_err = f"session_expired on {account_name}"
                    if not await self._rotate_account():
                        break
                    browser_restart_count = 0
                    retries_on_this_acc = 0
                    await asyncio.sleep(ROTATE_DELAY_S)
                    continue

                # ── Response validation + retry logic (poin 3) ──────────────────
                is_valid, parsed, validation_err = self._validate_qwen_response(response_text)

                if is_valid:
                    # Tandai di log kalau response asli sebenarnya broken JSON dan
                    # baru valid setelah _repair_unescaped_quotes() menyelamatkannya
                    # (mis. Qwen lupa escape tanda kutip di dalam `content`).
                    try:
                        json.loads(response_text)
                    except json.JSONDecodeError:
                        self.logger.info(
                            "Response diselamatkan oleh repair-fallback (unescaped quotes) "
                            "pada attempt %d – tidak perlu retry/rotasi akun",
                            attempt,
                        )

                if not is_valid:
                    self.logger.warning(
                        "Response tidak valid (attempt %d): %s | raw[:200]=%s",
                        attempt, validation_err, response_text[:200],
                    )

                    if total_retries >= HARD_CAP_RETRIES:
                        self.logger.error("Hard cap retry (%d) tercapai – hentikan", HARD_CAP_RETRIES)
                        break

                    total_retries += 1
                    retries_on_this_acc += 1

                    if retries_on_this_acc > MAX_RETRY_PER_ACC:
                        # Sudah 2 retry di akun ini → rotasi ke akun lain
                        self.logger.warning(
                            "Max retry per-akun (%d) tercapai di '%s' – rotasi akun",
                            MAX_RETRY_PER_ACC, account_name,
                        )
                        previous_account_err = (
                            f"invalid_response ({validation_err}) on {account_name}"
                        )
                        if not await self._rotate_account():
                            break
                        browser_restart_count = 0
                        retries_on_this_acc = 0
                        await asyncio.sleep(ROTATE_DELAY_S)
                        mode = "new"
                        continue

                    # Kirim corrective feedback dalam session yang SAMA
                    # Deteksi mode berdasarkan raw response: jika mengandung
                    # "tool_calls" berarti Qwen mencoba call tool tapi JSON-nya
                    # malformed → minta ulang format tool_calls, bukan success.
                    if '"tool_calls"' in response_text or '"status":"tool_calls"' in response_text:
                        corrective_prompt = (
                            "Response JSON kamu tidak valid karena ada karakter yang tidak di-escape "
                            "di dalam string (misalnya tanda kutip di dalam command). "
                            "Tolong ulangi response tool_calls kamu dengan memastikan semua "
                            "karakter spesial di dalam string value di-escape dengan benar "
                            "(gunakan \\\" untuk tanda kutip, \\n untuk newline). "
                            "Balas HANYA dengan JSON ini tanpa teks lain:\n"
                            '{"status":"tool_calls","tool_calls":[{"id":"call_<unique_id>","type":"function",'
                            '"function":{"name":"<tool_name>","arguments":{<args_as_valid_json_object>}}}]}'
                        )
                    else:
                        corrective_prompt = (
                            "Tolong ulangi response kamu dalam format JSON berikut, "
                            "tanpa teks lain di luar JSON:\n"
                            '{"status":"success","choices":[{"index":0,'
                            '"message":{"role":"assistant","content":"<isi jawaban kamu>"},'
                            '"finish_reason":"stop"}]}'
                        )
                    self.logger.info(
                        "Mengirim corrective feedback (retry %d/%d) dalam session yang sama",
                        total_retries, HARD_CAP_RETRIES,
                    )
                    await asyncio.sleep(RETRY_DELAY_S)
                    try:
                        corrective_extra = self._extra_send_kwargs()
                        # wrap_as_user_request=False: corrective_prompt adalah instruksi
                        # koreksi format dari sistem, bukan request baru dari user —
                        # jangan dibungkus [SYSTEM CONTEXT]/[USER REQUEST] lagi.
                        response_text = await self.send_prompt(
                            corrective_prompt, mode="continue",
                            wrap_as_user_request=False, **corrective_extra,
                        )
                    except Exception as corr_exc:
                        self.logger.warning("Corrective send gagal: %s", corr_exc)
                        attempt -= 1
                        continue

                    is_valid2, parsed2, validation_err2 = self._validate_qwen_response(response_text)
                    if not is_valid2:
                        self.logger.warning(
                            "Corrective response juga tidak valid: %s", validation_err2
                        )
                        attempt -= 1
                        continue
                    try:
                        json.loads(response_text)
                    except json.JSONDecodeError:
                        self.logger.info(
                            "Corrective response diselamatkan oleh repair-fallback "
                            "(unescaped quotes) pada attempt %d", attempt,
                        )

                    is_valid = True
                    parsed = parsed2
                    self.logger.info("Corrective berhasil – response valid setelah feedback")
                # ─────────────────────────────────────────────────────────────────

                # ─────────────────────────────────────────────────────────────────

                # ── Extract content atau tool_calls dari parsed JSON ──────────────────
                # Jika status "tool_calls" → Qwen minta panggil tool, return sekarang.
                if parsed.get("status") == "tool_calls":
                    tool_calls_list = parsed.get("tool_calls", [])
                    self.logger.info(
                        "Qwen response: tool_calls (%d call(s))", len(tool_calls_list)
                    )
                    return {
                        "success":       True,
                        "finish_reason": "tool_calls",
                        "tool_calls":    tool_calls_list,
                        "response":      None,
                        "usage":         {},
                        "x_metadata":    {},
                    }
                content_str = parsed["choices"][0]["message"]["content"]

                # ── tiktoken usage (poin 5) ──────────────────────────────────────
                prompt_tokens     = _count_tokens(prompt)
                completion_tokens = _count_tokens(content_str)
                total_tokens      = prompt_tokens + completion_tokens

                # ── Metadata inject (poin 4) ─────────────────────────────────────
                response_time_ms = int((_time.monotonic() - scrape_start_time) * 1000)
                cookie_file_path = (
                    str(self._current_cookie_file) if self._current_cookie_file else ""
                )
                try:
                    cookie_files_list = list(getattr(self, "_cookie_files", []))
                    acc_index = next(
                        (i for i, cf in enumerate(cookie_files_list)
                         if cf.stem == account_name),
                        0,
                    )
                except Exception:
                    acc_index = 0

                effective_think = getattr(self, "_think_mode", None)

                x_metadata: dict = {
                    "model":            account_name,
                    "account_file":     cookie_file_path,
                    "account_index":    acc_index,
                    "timestamp":        int(_time.time()),
                    "account_status":   "ok",
                    "retry_count":      total_retries,
                    "response_time_ms": response_time_ms,
                    "think_mode":       effective_think,
                }
                if previous_account_err:
                    x_metadata["previous_account_error"] = previous_account_err
                # ─────────────────────────────────────────────────────────────────

                blocks = self.extract_code_blocks(content_str)
                result = {
                    "prompt":           prompt,
                    "response":         content_str,
                    "file_type":        self.detect_file_type(content_str),
                    "code_blocks":      blocks,
                    "code_block_count": len(blocks),
                    "account_used":     account_name,
                    "timestamp":        datetime.now().isoformat(),
                    "success":          True,
                    "finish_reason":    "stop",
                    "error":            None,
                    "usage": {
                        "prompt_tokens":     prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens":      total_tokens,
                    },
                    "x_metadata": x_metadata,
                }
                self.logger.info(
                    "Scrape successful – %d char(s), %d code block(s) | "
                    "tokens p=%d c=%d | retries=%d | %dms",
                    len(content_str), len(blocks),
                    prompt_tokens, completion_tokens,
                    total_retries, response_time_ms,
                )
                return result

            except TimeoutError as exc:
                self.logger.error("Timeout on attempt %d: %s", attempt, exc)
                await self.take_debug_screenshot(f"timeout_attempt{attempt}")
                if await self._is_page_crashed():
                    self.logger.warning("Timeout caused by page crash – restarting browser")
                    if browser_restart_count < max_browser_restarts:
                        browser_restart_count += 1
                        restarted = await self.restart_browser()
                        if restarted:
                            attempt -= 1
                            continue
                else:
                    retry_sleep(ROTATION_CONFIG["retry_delay"])

            except Exception as exc:
                self.logger.error("Error on attempt %d: %s", attempt, exc, exc_info=True)
                await self.take_debug_screenshot(f"error_attempt{attempt}")
                if await self._is_page_crashed():
                    self.logger.warning("Exception caused by page crash – restarting browser")
                    if browser_restart_count < max_browser_restarts:
                        browser_restart_count += 1
                        restarted = await self.restart_browser()
                        if restarted:
                            attempt -= 1
                            continue
                elif await self.is_rate_limited() or await self.is_session_expired():
                    if not await self._rotate_account():
                        break
                    browser_restart_count = 0
                    retries_on_this_acc = 0
                    previous_account_err = f"exception on {account_name}: {exc}"
                else:
                    retry_sleep(ROTATION_CONFIG["retry_delay"])

        # Semua attempt habis — ambil screenshot kondisi akhir browser
        await self.take_debug_screenshot("all_attempts_exhausted")

        return {
            "prompt":           prompt,
            "response":         None,
            "file_type":        None,
            "code_blocks":      [],
            "code_block_count": 0,
            "account_used":     None,
            "timestamp":        datetime.now().isoformat(),
            "success":          False,
            "error":            "All attempts exhausted",
            "usage":            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "x_metadata":       {},
        }

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "BaseAIChatScraper":
        # Discover accounts FIRST so _cookie_files is populated before launch
        self._discover_accounts()
        # Pass the initial cookie file so the correct profile is opened
        initial_cookie = self.cookies_path or (
            self._cookie_files[self._cookie_index]
            if self._cookie_files
            else None
        )
        await self.launch_browser(cookie_file=initial_cookie)
        return self

    async def __aexit__(self, *_) -> None:
        await self.close_browser()