"""
browser_pool_chatgpt.py — Pre-warmed Browser Pool untuk ChatGPTScraper.
========================================================================

AUTH MODEL (identik dengan Qwen/DeepSeek email+password mode):
  • Semua account didefinisikan di cookies/authchatgpt.json
    Format: [{"name": "account1", "email": "...", "password": "..."}]
  • Setiap account → persistent browser profile di profiles/chatgpt/<account>/
  • Saat warmup (start()), setiap slot:
      1. launch_browser(account) → buka persistent context
      2. ensure_authenticated():
           - Profile lama & session valid  → langsung siap (tanpa login)
           - Profile baru / expired        → login otomatis (headless=true,
             via "Continue with password")

Deviasi dari browser_pool_qwen.py (per design v1, Tahap D):
  • TIDAK ada legacy cookie-file mode (ChatGPT hanya auth.json).
  • TIDAK ada think_mode / no-headless-per-slot runtime toggle.
  • 1 slot per account (pool_size = len(accounts) secara default; bisa
    di-override tapi tetap wrap round-robin bila pool_size > jumlah account).
  • run_task() TIDAK melakukan goto()/skip-goto optimisation sendiri —
    ChatGPTScraper.send_prompt() sudah menangani navigasi NEW/CONTINUE
    (termasuk continue_url) di dalam scrape(), jadi pool hanya
    acquire → scrape → release.

Usage (di public_chatgpt.py):
    pool = BrowserPool(pool_size=2, headless=True)
    await pool.start()

    result = await pool.run_task(prompt, mode="new")

    await pool.stop()
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import AsyncIterator, Optional

from config import CHATGPT_AUTH_CONFIG
from scrapers.chatgpt_scraper import ChatGPTScraper
from scrapers.utils import AuthStore, setup_logger

logger = setup_logger("browser_pool_chatgpt")


# ─── Slot Status ──────────────────────────────────────────────────────── #

class SlotStatus(Enum):
    STARTING = auto()   # sedang diinisialisasi / respawn
    IDLE     = auto()   # siap dipakai
    BUSY     = auto()   # sedang dipakai oleh satu task
    DEAD     = auto()   # crash, menunggu respawn


# ─── BrowserSlot ──────────────────────────────────────────────────────── #

@dataclass
class BrowserSlot:
    slot_id: int
    account_name: str
    scraper: Optional[ChatGPTScraper] = None
    status: SlotStatus = SlotStatus.STARTING
    last_used: float = field(default_factory=time.time)
    error_count: int = 0

    def mark_busy(self) -> None:
        self.status = SlotStatus.BUSY
        self.last_used = time.time()

    def mark_idle(self) -> None:
        self.status = SlotStatus.IDLE
        self.last_used = time.time()
        self.error_count = 0

    def mark_dead(self) -> None:
        self.status = SlotStatus.DEAD
        self.error_count += 1


# ─── BrowserPool ──────────────────────────────────────────────────────── #

class BrowserPool:
    MAX_RESPAWN_ATTEMPTS = 3
    RESPAWN_DELAY = 5.0
    ACQUIRE_POLL = 0.3

    def __init__(self, pool_size: int | None = None, headless: bool = True) -> None:
        self.headless = headless

        auth_store = AuthStore(CHATGPT_AUTH_CONFIG["auth_file"])
        self._auth_accounts: list[str] = auth_store.account_names()

        if not self._auth_accounts:
            raise RuntimeError(
                f"Tidak ada account di {CHATGPT_AUTH_CONFIG['auth_file']}.\n"
                f"Buat file tersebut dengan format:\n"
                f'  [{{"name": "account1", "email": "you@email.com", "password": "secret"}}]\n'
                f"Lihat .env.example untuk petunjuk lengkap."
            )

        # Default: 1 slot per account.
        self.pool_size = pool_size if pool_size else len(self._auth_accounts)

        logger.info(
            "BrowserPool(chatgpt): authchatgpt.json — %d account(s): %s, pool_size=%d",
            len(self._auth_accounts), self._auth_accounts, self.pool_size,
        )

        self._slots: list[BrowserSlot] = []
        self._lock = asyncio.Lock()
        self._idle_event = asyncio.Event()
        self._started = False
        self._rr_index: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────── #

    async def start(self) -> None:
        if self._started:
            return

        accounts = self._auth_accounts
        for i in range(self.pool_size):
            acct = accounts[i % len(accounts)]
            self._slots.append(BrowserSlot(slot_id=i, account_name=acct))

        await asyncio.gather(*[self._init_slot(slot) for slot in self._slots])

        idle_count = sum(1 for s in self._slots if s.status == SlotStatus.IDLE)
        logger.info("BrowserPool(chatgpt): %d/%d slot berhasil IDLE", idle_count, self.pool_size)
        if idle_count == 0:
            raise RuntimeError(
                "Tidak ada slot ChatGPT yang berhasil diinisialisasi. "
                "Cek credentials di authchatgpt.json dan koneksi internet."
            )
        self._started = True

    async def stop(self) -> None:
        logger.info("BrowserPool(chatgpt): menutup semua slot...")

        async def _close(slot: BrowserSlot) -> None:
            if slot.scraper:
                try:
                    await slot.scraper.close_browser()
                except Exception as exc:
                    logger.warning("Slot#%d close error: %s", slot.slot_id, exc)
                finally:
                    slot.scraper = None
                    slot.status = SlotStatus.DEAD

        await asyncio.gather(*[_close(s) for s in self._slots])
        self._started = False
        logger.info("BrowserPool(chatgpt): semua slot ditutup")

    # ── Slot initialization ──────────────────────────────────────── #

    async def _init_slot(self, slot: BrowserSlot) -> None:
        slot.status = SlotStatus.STARTING
        account = slot.account_name
        try:
            logger.info("Slot#%d: warming up account '%s' (headless=%s) …",
                        slot.slot_id, account, self.headless)

            scraper = ChatGPTScraper(headless=self.headless, account=account)
            await scraper.launch_browser(account=account)

            ok = await scraper.ensure_authenticated()
            if not ok:
                raise RuntimeError(
                    f"Autentikasi ChatGPT gagal untuk account '{account}'. "
                    f"Periksa credentials di {CHATGPT_AUTH_CONFIG['auth_file']}."
                )

            logger.info("Slot#%d ✅ siap (account: %s)", slot.slot_id, account)
            slot.scraper = scraper
            slot.mark_idle()
            self._idle_event.set()
        except Exception as exc:
            slot.mark_dead()
            logger.error("Slot#%d ❌ gagal init (account: %s): %s",
                         slot.slot_id, account, exc, exc_info=True)

    def _schedule_respawn(self, slot: BrowserSlot) -> None:
        asyncio.create_task(self._respawn_slot(slot))

    async def _respawn_slot(self, slot: BrowserSlot) -> None:
        if slot.error_count >= self.MAX_RESPAWN_ATTEMPTS:
            logger.error(
                "Slot#%d melebihi MAX_RESPAWN_ATTEMPTS (%d) — slot dinonaktifkan permanen",
                slot.slot_id, self.MAX_RESPAWN_ATTEMPTS,
            )
            slot.status = SlotStatus.DEAD
            return

        logger.warning("Slot#%d 🔄 respawn (attempt %d/%d)...",
                       slot.slot_id, slot.error_count + 1, self.MAX_RESPAWN_ATTEMPTS)

        if slot.scraper:
            try:
                await slot.scraper.close_browser()
            except Exception:
                pass
            slot.scraper = None

        await asyncio.sleep(self.RESPAWN_DELAY)
        await self._init_slot(slot)

    # ── Acquire / Release ────────────────────────────────────────── #

    @asynccontextmanager
    async def acquire(
        self,
        preferred_account: str | None = None,
        timeout: float = 120.0,
    ) -> AsyncIterator[BrowserSlot]:
        deadline = time.monotonic() + timeout
        slot = await self._wait_for_idle_slot(preferred_account, deadline)
        slot.mark_busy()

        try:
            if slot.scraper and await slot.scraper._is_page_crashed():
                logger.warning("Slot#%d: crash terdeteksi saat acquire", slot.slot_id)
                slot.mark_dead()
                self._schedule_respawn(slot)
                raise RuntimeError(f"Slot#{slot.slot_id} crashed on acquire")
        except RuntimeError:
            raise
        except Exception:
            pass

        try:
            yield slot
        except Exception as exc:
            logger.error("Slot#%d error saat dipakai: %s", slot.slot_id, exc)
            slot.mark_dead()
            self._schedule_respawn(slot)
            raise
        else:
            if slot.status != SlotStatus.DEAD:
                slot.mark_idle()
                self._idle_event.set()

    async def _wait_for_idle_slot(
        self, preferred_account: str | None, deadline: float,
    ) -> BrowserSlot:
        while True:
            async with self._lock:
                if preferred_account:
                    matched = [s for s in self._slots if s.account_name == preferred_account]
                    idle_match = next(
                        (s for s in matched if s.status == SlotStatus.IDLE and s.scraper), None,
                    )
                    if idle_match:
                        return idle_match
                    if not matched:
                        logger.warning(
                            "Preferred account '%s' tidak ditemukan di pool — fallback ke slot mana saja",
                            preferred_account,
                        )
                    else:
                        self._idle_event.clear()
                else:
                    n = len(self._slots)
                    for i in range(n):
                        candidate = self._slots[(self._rr_index + i) % n]
                        if candidate.status == SlotStatus.IDLE and candidate.scraper:
                            self._rr_index = (self._slots.index(candidate) + 1) % n
                            return candidate
                    self._idle_event.clear()

            if time.monotonic() >= deadline:
                raise TimeoutError("No available ChatGPT worker slot within timeout")

            try:
                await asyncio.wait_for(self._idle_event.wait(), timeout=self.ACQUIRE_POLL)
            except asyncio.TimeoutError:
                pass

    # ── Task execution ────────────────────────────────────────────── #

    async def run_task(
        self,
        prompt: str,
        *,
        mode: str = "new",
        attachments: list | None = None,
        continue_url: str | None = None,
        preferred_account: str | None = None,
        acquire_timeout: float = 120.0,
    ) -> dict:
        """Acquire a slot (pinned to preferred_account when given), scrape,
        release. On rate-limit in mode='new', rotate account and retry once.
        """
        async with self.acquire(preferred_account, timeout=acquire_timeout) as slot:
            result = await slot.scraper.scrape(
                prompt, mode=mode, attachments=attachments, continue_url=continue_url,
            )

            if not result.get("ok") and mode == "new":
                err = (result.get("error") or "").lower()
                if "rate limit" in err or "usage cap" in err:
                    logger.warning(
                        "Slot#%d rate-limited — rotating account and retrying once", slot.slot_id,
                    )
                    rotated = await slot.scraper._rotate_account()
                    if rotated:
                        result = await slot.scraper.scrape(
                            prompt, mode=mode, attachments=attachments, continue_url=continue_url,
                        )
            elif not result.get("ok") and mode == "continue":
                # Session terikat akun — rotasi akan kehilangan konteks percakapan.
                logger.error(
                    "Slot#%d task gagal dalam mode continue (tidak dirotasi, "
                    "akan kehilangan konteks): %s", slot.slot_id, result.get("error"),
                )

            return result

    # ── Account management / diagnostics ─────────────────────────── #

    def add_account(self, account_name: str) -> None:
        """Register a new account name from authchatgpt.json into the pool
        (runtime addition — actual browser init happens lazily via
        LocalWorker._add_account_runtime, mirroring DeepSeek's pattern)."""
        name = account_name.strip()
        auth_store = AuthStore(CHATGPT_AUTH_CONFIG["auth_file"])
        all_accounts = auth_store.account_names()
        if name not in all_accounts:
            raise ValueError(
                f"Account '{name}' tidak ditemukan di {CHATGPT_AUTH_CONFIG['auth_file']}. "
                f"Account tersedia: {all_accounts}"
            )
        for slot in self._slots:
            if slot.account_name == name:
                raise ValueError(f"Account '{name}' sudah terdaftar di Slot#{slot.slot_id}")

        new_slot_id = max((s.slot_id for s in self._slots), default=-1) + 1
        slot = BrowserSlot(slot_id=new_slot_id, account_name=name)
        self._slots.append(slot)
        asyncio.create_task(self._init_slot(slot))

    def list_accounts(self) -> list[dict]:
        return [
            {"account": s.account_name, "status": s.status.name.lower(), "slot_id": s.slot_id}
            for s in self._slots
        ]

    def busy_accounts(self) -> list[str]:
        return [s.account_name for s in self._slots if s.status == SlotStatus.BUSY]

    def status_summary(self) -> dict:
        counts = {s: 0 for s in SlotStatus}
        for slot in self._slots:
            counts[slot.status] += 1
        return {
            "total": len(self._slots),
            "idle": counts[SlotStatus.IDLE],
            "busy": counts[SlotStatus.BUSY],
            "starting": counts[SlotStatus.STARTING],
            "dead": counts[SlotStatus.DEAD],
        }

    @property
    def slots(self) -> list[BrowserSlot]:
        return self._slots
