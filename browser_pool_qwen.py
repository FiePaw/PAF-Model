"""
browser_pool.py – Pre-warmed Browser Pool untuk AIChatScraper
=============================================================

Setiap slot di pool:
  • Dedicated ke 1 cookie file
  • Browser + halaman sudah terbuka & login sejak startup
  • Task tinggal langsung send_prompt() tanpa cold-start

Usage (di public.py):
    pool = BrowserPool(cookies_dir=COOKIES_DIR, pool_size=20, headless=True)
    await pool.start()

    async with pool.acquire() as scraper:
        result = await scraper.send_prompt(prompt)

    await pool.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import AsyncIterator

from scrapers.qwen_scraper import QwenScraper
from scrapers.utils import discover_cookie_files, setup_logger

logger = setup_logger("browser_pool")


# ─── Slot Status ──────────────────────────────────────────────────────────────

class SlotStatus(Enum):
    STARTING  = auto()   # sedang diinisialisasi / respawn
    IDLE      = auto()   # siap dipakai
    BUSY      = auto()   # sedang dipakai oleh satu task
    DEAD      = auto()   # crash, menunggu respawn


# ─── BrowserSlot ──────────────────────────────────────────────────────────────

@dataclass
class BrowserSlot:
    slot_id: int
    cookie_file: Path
    scraper: QwenScraper | None = None
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


# ─── BrowserPool ──────────────────────────────────────────────────────────────

class BrowserPool:
    """
    Pool of pre-warmed QwenScraper instances.

    • pool_size browser dibuat saat start(), masing-masing pakai 1 cookie file.
    • Kalau jumlah cookie file < pool_size, cookie di-wrap round-robin.
    • acquire() mengembalikan slot idle; kalau semua busy, tunggu sampai ada yang selesai.
    • Slot yang crash di-respawn otomatis di background dengan cookie yang sama.
    """

    MAX_RESPAWN_ATTEMPTS = 3       # maks percobaan respawn sebelum slot dianggap permanen mati
    RESPAWN_DELAY        = 5.0     # detik jeda sebelum respawn
    ACQUIRE_POLL         = 0.3     # detik polling saat semua slot busy
    WARMUP_NAVIGATE      = True    # navigasi ke chat.qwen.ai saat warmup

    def __init__(
        self,
        cookies_dir: Path,
        pool_size: int = 4,
        headless: bool = True,
        think_mode: str | None = None,
    ) -> None:
        self.cookies_dir = Path(cookies_dir)
        self.pool_size   = pool_size
        self.headless    = headless
        self.think_mode  = think_mode

        self._slots: list[BrowserSlot] = []
        # Kumpulan slot_id yang sedang berjalan dalam mode no-headless.
        # Dipakai oleh restart_slot_no_headless() dan stop_all_no_headless().
        self._no_headless_slot_ids: set[int] = set()
        self._lock  = asyncio.Lock()          # untuk modifikasi _slots
        self._idle_event = asyncio.Event()    # di-set setiap kali ada slot → IDLE
        self._started = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn semua slot secara paralel lalu tunggu hingga semuanya IDLE."""
        if self._started:
            return

        cookie_files = discover_cookie_files(self.cookies_dir)
        if not cookie_files:
            raise RuntimeError(f"Tidak ada cookie file di {self.cookies_dir}")

        logger.info(
            "BrowserPool: memulai %d slot (cookie file tersedia: %d)",
            self.pool_size, len(cookie_files),
        )

        # Assign cookie ke slot; wrap round-robin jika cookie < pool_size
        for i in range(self.pool_size):
            cf = cookie_files[i % len(cookie_files)]
            slot = BrowserSlot(slot_id=i, cookie_file=cf)
            self._slots.append(slot)

        # Spawn semua browser paralel
        await asyncio.gather(*[self._init_slot(slot) for slot in self._slots])

        idle_count = sum(1 for s in self._slots if s.status == SlotStatus.IDLE)
        logger.info(
            "BrowserPool: %d/%d slot berhasil IDLE",
            idle_count, self.pool_size,
        )
        if idle_count == 0:
            raise RuntimeError("Tidak ada slot yang berhasil diinisialisasi")

        self._started = True

    async def stop(self) -> None:
        """Tutup semua browser dengan graceful."""
        logger.info("BrowserPool: menutup semua slot...")
        async def _close(slot: BrowserSlot) -> None:
            if slot.scraper:
                try:
                    await slot.scraper.close_browser()
                except Exception as e:
                    logger.warning("Slot#%d close error: %s", slot.slot_id, e)
                finally:
                    slot.scraper = None
                    slot.status = SlotStatus.DEAD

        await asyncio.gather(*[_close(s) for s in self._slots])
        self._started = False
        logger.info("BrowserPool: semua slot ditutup")

    # ── Slot initialization ───────────────────────────────────────────────────

    async def _init_slot(self, slot: BrowserSlot) -> None:
        """Buat scraper baru untuk slot, navigasi ke halaman chat."""
        slot.status = SlotStatus.STARTING
        try:
            scraper = QwenScraper(
                headless=self.headless,
                cookies_path=slot.cookie_file,
                cookies_dir=self.cookies_dir,
                think_mode=self.think_mode,
            )
            # Bootstrap: discover accounts + launch browser (with cookie seeding)
            scraper._discover_accounts()

            # FIX: set _cookie_index ke posisi slot.cookie_file di dalam list.
            # Tanpa ini, _cookie_index selalu 0 sehingga scrape() selalu log
            # "account1" meskipun browser-nya pakai cookie yang berbeda.
            try:
                idx = scraper._cookie_files.index(slot.cookie_file)
                scraper._cookie_index = idx
            except ValueError:
                # Edge case: path tidak cocok persis di list. Set cookies_path
                # sebagai fallback agar _current_cookie_file tidak salah baca.
                scraper.cookies_path = slot.cookie_file
                logger.warning(
                    "Slot#%d: %s tidak ditemukan di _cookie_files, fallback cookies_path",
                    slot.slot_id, slot.cookie_file.name,
                )

            await scraper.launch_browser(cookie_file=slot.cookie_file)

            slot.scraper = scraper
            slot.mark_idle()
            self._idle_event.set()
            logger.info(
                "Slot#%d ✅ siap (cookie: %s)",
                slot.slot_id, slot.cookie_file.name,
            )
        except Exception as e:
            slot.mark_dead()
            logger.error("Slot#%d ❌ gagal init: %s", slot.slot_id, e, exc_info=True)

    # ── Respawn ───────────────────────────────────────────────────────────────

    def _schedule_respawn(self, slot: BrowserSlot) -> None:
        """Fire-and-forget: respawn slot di background."""
        asyncio.create_task(self._respawn_slot(slot))

    async def _respawn_slot(self, slot: BrowserSlot) -> None:
        """Tutup browser lama, lalu init ulang dengan cookie yang sama."""
        if slot.error_count >= self.MAX_RESPAWN_ATTEMPTS:
            logger.error(
                "Slot#%d melebihi MAX_RESPAWN_ATTEMPTS (%d) – slot dinonaktifkan permanen",
                slot.slot_id, self.MAX_RESPAWN_ATTEMPTS,
            )
            slot.status = SlotStatus.DEAD
            return

        logger.warning(
            "Slot#%d 🔄 respawn (attempt %d/%d)...",
            slot.slot_id, slot.error_count + 1, self.MAX_RESPAWN_ATTEMPTS,
        )

        # Tutup browser lama
        if slot.scraper:
            try:
                await slot.scraper.close_browser()
            except Exception:
                pass
            slot.scraper = None

        await asyncio.sleep(self.RESPAWN_DELAY)
        await self._init_slot(slot)

    # ── Acquire / Release ─────────────────────────────────────────────────────

    @asynccontextmanager
    async def acquire(
        self,
        preferred_cookie: str | None = None,
        preferred_slot_id: int | None = None,
    ) -> AsyncIterator[tuple[QwenScraper, str, int]]:
        """
        Context manager: pinjam satu slot IDLE, kembalikan otomatis setelah selesai.

        preferred_slot_id: slot_id spesifik yang diminta (mode CONTINUE optimal).
            Kalau diberikan, langsung tunggu slot dengan ID ini tanpa cari yang lain.
            Ini menghindari goto() ulang karena browser sudah di halaman yang benar.

        preferred_cookie: nama file cookie (misal "acc1.json") yang diprioritaskan.
            Dipakai sebagai fallback jika preferred_slot_id tidak diberikan.
            Kalau slot dengan cookie tersebut sedang BUSY, tunggu sampai slot itu
            idle — TIDAK mengambil slot dengan cookie berbeda.
            Kalau preferred_cookie=None (mode NEW), ambil slot idle mana saja.

        Yield: tuple (scraper, cookie_file_name, slot_id)

        async with pool.acquire(preferred_slot_id=2) as (scraper, cookie_name, slot_id):
            result = await scraper.scrape(prompt)
        """
        while True:
            slot = await self._wait_for_idle_slot(
                preferred_cookie=preferred_cookie,
                preferred_slot_id=preferred_slot_id,
            )
            slot.mark_busy()

            # ── Cek page crash sebelum slot diserahkan ke task ────────────────
            # Jika crash terdeteksi, tandai DEAD, jadwalkan respawn, dan cari slot lain.
            try:
                if slot.scraper and await slot.scraper._is_page_crashed():
                    logger.warning(
                        "Slot#%d: crash terdeteksi saat acquire – marking DEAD dan cari slot lain",
                        slot.slot_id,
                    )
                    slot.mark_dead()
                    self._schedule_respawn(slot)
                    continue   # balik ke while True, cari slot idle lain
            except Exception:
                pass  # jika cek crash sendiri gagal, lanjutkan (biarkan task yang handle)

            logger.debug(
                "Slot#%d dipakai (cookie=%s, preferred_slot=%s, preferred_cookie=%s)",
                slot.slot_id, slot.cookie_file.name,
                preferred_slot_id if preferred_slot_id is not None else "-",
                preferred_cookie or "-",
            )
            break   # slot sehat, keluar dari loop

        try:
            yield slot.scraper, slot.cookie_file.name, slot.slot_id
        except Exception as e:
            logger.error("Slot#%d error saat dipakai: %s", slot.slot_id, e)
            slot.mark_dead()
            self._schedule_respawn(slot)
            raise
        else:
            await self._reset_slot_page(slot)
            # Jika _reset_slot_page menandai slot DEAD (crash post-task),
            # jangan paksa ke IDLE lagi — respawn sudah dijadwalkan.
            if slot.status != SlotStatus.DEAD:
                slot.mark_idle()
                self._idle_event.set()
                logger.debug("Slot#%d kembali idle", slot.slot_id)

    async def _wait_for_idle_slot(
        self,
        preferred_cookie: str | None = None,
        preferred_slot_id: int | None = None,
    ) -> BrowserSlot:
        """
        Tunggu sampai ada slot IDLE yang sesuai, lalu return slot tersebut.

        Logika pemilihan slot (prioritas urutan):
        1. preferred_slot_id diberikan (mode CONTINUE optimal):
             Langsung tunggu slot dengan ID ini — browser sudah di halaman yang benar,
             tidak perlu goto() ulang.
        2. preferred_cookie diberikan (mode CONTINUE fallback):
             Tunggu slot dengan cookie_file.name == preferred_cookie.
        3. Keduanya None (mode NEW):
             Ambil slot idle mana saja (paling lama idle).
        """
        while True:
            async with self._lock:

                # ── Prioritas 1: slot_id spesifik ────────────────────────────
                if preferred_slot_id is not None:
                    target = next(
                        (s for s in self._slots if s.slot_id == preferred_slot_id),
                        None,
                    )
                    if target and target.status == SlotStatus.IDLE and target.scraper:
                        return target
                    if target and target.status == SlotStatus.DEAD:
                        # Slot mati → fallback ke cookie
                        logger.warning(
                            "Slot#%d mati, fallback ke preferred_cookie=%s",
                            preferred_slot_id, preferred_cookie,
                        )
                        preferred_slot_id = None   # lanjut ke logika cookie
                    else:
                        # Slot ada tapi BUSY → tunggu
                        self._idle_event.clear()
                        await asyncio.sleep(self.ACQUIRE_POLL)
                        continue

                # ── Prioritas 2: cookie spesifik ─────────────────────────────
                if preferred_cookie:
                    matched_slots = [
                        s for s in self._slots
                        if s.cookie_file.name == preferred_cookie
                    ]
                    if not matched_slots:
                        logger.warning(
                            "Cookie '%s' tidak ditemukan di pool – fallback ke slot mana saja",
                            preferred_cookie,
                        )
                    else:
                        idle_match = next(
                            (s for s in matched_slots if s.status == SlotStatus.IDLE and s.scraper),
                            None,
                        )
                        if idle_match:
                            return idle_match
                        self._idle_event.clear()
                        await asyncio.sleep(self.ACQUIRE_POLL)
                        continue

                # ── Prioritas 3: slot idle mana saja (mode NEW) ───────────────
                idle_slots = [
                    s for s in self._slots
                    if s.status == SlotStatus.IDLE and s.scraper
                ]
                if idle_slots:
                    return min(idle_slots, key=lambda s: s.last_used)

                self._idle_event.clear()

            # Tidak ada slot idle — tunggu event lalu poll lagi
            try:
                await asyncio.wait_for(self._idle_event.wait(), timeout=self.ACQUIRE_POLL)
            except asyncio.TimeoutError:
                pass

    async def _reset_slot_page(self, slot: BrowserSlot) -> None:
        """
        Reset state scraper setelah task selesai.

        Jika halaman crash terdeteksi setelah task, tandai slot sebagai DEAD
        dan jadwalkan respawn — slot tidak akan diberi task baru sampai browser-nya sehat.
        """
        if not slot.scraper:
            return

        try:
            # Cek apakah halaman crash setelah task selesai
            if await slot.scraper._is_page_crashed():
                logger.warning(
                    "Slot#%d: page crash terdeteksi setelah task selesai – scheduling respawn",
                    slot.slot_id,
                )
                slot.mark_dead()
                self._schedule_respawn(slot)
                return

            slot.scraper._conversation_started = False
            slot.scraper._think_mode_applied = False

        except Exception as e:
            logger.warning("Slot#%d reset page error: %s", slot.slot_id, e)

    def get_cookie_path(self, cookie_name: str) -> Path:
        """
        Kembalikan Path cookie file berdasarkan nama file.
        Dipakai TaskProcessor untuk menyimpan Path ke Session setelah task NEW.
        Kalau tidak ketemu, kembalikan Path dari cookies_dir (best-effort).
        """
        for slot in self._slots:
            if slot.cookie_file.name == cookie_name:
                return slot.cookie_file
        # fallback
        return self.cookies_dir / cookie_name

    # ── Status / Diagnostics ──────────────────────────────────────────────────

    def status_summary(self) -> dict:
        counts = {s: 0 for s in SlotStatus}
        for slot in self._slots:
            counts[slot.status] += 1
        return {
            "total"   : len(self._slots),
            "idle"    : counts[SlotStatus.IDLE],
            "busy"    : counts[SlotStatus.BUSY],
            "starting": counts[SlotStatus.STARTING],
            "dead"    : counts[SlotStatus.DEAD],
        }

    # ── Account command helpers ───────────────────────────────────────────────

    async def add_account(self, cookie_filename: str) -> dict:
        """
        Daftarkan akun baru ke pool secara runtime tanpa restart worker.

        Alur:
          1. Resolve path: cookies_dir / cookie_filename
             (boleh dengan atau tanpa ekstensi .json)
          2. Validasi file ada dan belum terdaftar di pool.
          3. Buat BrowserSlot baru dengan slot_id berikutnya.
          4. Jalankan _init_slot → browser langsung warm & IDLE.

        Returns dict {"ok": bool, "message": str, "slot_id": int | None}.
        """
        # Normalisasi nama file: pastikan berekstensi .json
        name = cookie_filename.strip()
        if not name.endswith(".json"):
            name += ".json"

        cookie_path = self.cookies_dir / name

        # Validasi: file harus ada
        if not cookie_path.exists():
            return {
                "ok"     : False,
                "slot_id": None,
                "message": f"File cookie tidak ditemukan: {cookie_path}",
            }

        # Validasi: belum terdaftar di pool
        for slot in self._slots:
            if slot.cookie_file.resolve() == cookie_path.resolve():
                return {
                    "ok"     : False,
                    "slot_id": slot.slot_id,
                    "message": (
                        f"Akun '{cookie_path.stem}' sudah terdaftar "
                        f"di Slot#{slot.slot_id} (status: {slot.status.name.lower()})"
                    ),
                }

        # Buat slot baru
        new_slot_id = max((s.slot_id for s in self._slots), default=-1) + 1
        slot = BrowserSlot(slot_id=new_slot_id, cookie_file=cookie_path)

        async with self._lock:
            self._slots.append(slot)

        logger.info(
            "addaccount: menambahkan Slot#%d untuk akun '%s' …",
            new_slot_id, cookie_path.stem,
        )

        await self._init_slot(slot)

        if slot.status == SlotStatus.IDLE:
            return {
                "ok"     : True,
                "slot_id": new_slot_id,
                "message": (
                    f"Akun '{cookie_path.stem}' berhasil ditambahkan "
                    f"sebagai Slot#{new_slot_id} dan siap digunakan"
                ),
            }
        else:
            return {
                "ok"     : False,
                "slot_id": new_slot_id,
                "message": (
                    f"Slot#{new_slot_id} untuk akun '{cookie_path.stem}' "
                    f"gagal inisialisasi (status: {slot.status.name.lower()})"
                ),
            }

    def list_accounts(self) -> list[dict]:
        """
        Kembalikan daftar semua akun beserta status slot-nya.

        Return contoh:
            [
                {"account": "account1", "status": "idle",  "slot_id": 0, "no_headless": False},
                {"account": "account2", "status": "busy",  "slot_id": 1, "no_headless": True},
            ]
        """
        return [
            {
                "account"    : slot.cookie_file.stem,
                "status"     : slot.status.name.lower(),
                "slot_id"    : slot.slot_id,
                "no_headless": slot.slot_id in self._no_headless_slot_ids,
            }
            for slot in self._slots
        ]

    def busy_accounts(self) -> list[dict]:
        """Kembalikan hanya akun yang sedang BUSY."""
        return [a for a in self.list_accounts() if a["status"] == "busy"]

    async def restart_slot_no_headless(self, account_name: str) -> dict:
        """
        Restart slot milik *account_name* dengan mode --no-headless (visible window).

        Alur:
          1. Cari slot dengan cookie_file.stem == account_name.
          2. Jika slot sedang BUSY → tolak (kembalikan error).
          3. Tutup browser lama, init ulang dengan headless=False.
          4. Tandai slot_id di _no_headless_slot_ids.

        Returns dict {"ok": bool, "message": str}.
        """
        target: BrowserSlot | None = None
        for slot in self._slots:
            if slot.cookie_file.stem == account_name:
                target = slot
                break

        if target is None:
            return {"ok": False, "message": f"Akun '{account_name}' tidak ditemukan"}

        if target.status == SlotStatus.BUSY:
            return {
                "ok": False,
                "message": f"Akun '{account_name}' sedang BUSY — tidak bisa direstart",
            }

        logger.info(
            "showheadless: restart Slot#%d (%s) → no-headless …",
            target.slot_id, account_name,
        )

        # Tutup browser lama
        if target.scraper:
            try:
                await target.scraper.close_browser()
            except Exception:
                pass
            target.scraper = None
        target.mark_dead()

        # Simpan override headless untuk slot ini sebelum _init_slot
        self._no_headless_slot_ids.add(target.slot_id)

        # Spawn scraper baru dengan headless=False (override sementara)
        target.status = SlotStatus.STARTING
        try:
            scraper = QwenScraper(
                headless=False,
                cookies_path=target.cookie_file,
                cookies_dir=self.cookies_dir,
                think_mode=self.think_mode,
            )
            scraper._discover_accounts()
            try:
                idx = scraper._cookie_files.index(target.cookie_file)
                scraper._cookie_index = idx
            except ValueError:
                scraper.cookies_path = target.cookie_file

            await scraper.launch_browser(cookie_file=target.cookie_file)

            # Navigasi ke halaman settings/general agar user bisa langsung
            # memeriksa kondisi akun secara visual di browser yang terbuka.
            settings_url = "https://chat.qwen.ai/settings/general"
            try:
                await scraper._page.goto(
                    settings_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                logger.info(
                    "showheadless: Slot#%d (%s) → navigasi ke %s berhasil",
                    target.slot_id, account_name, settings_url,
                )
            except Exception as nav_err:
                # Navigasi gagal tidak dianggap fatal — browser tetap terbuka
                logger.warning(
                    "showheadless: Slot#%d (%s) ⚠️  gagal navigasi ke %s: %s",
                    target.slot_id, account_name, settings_url, nav_err,
                )

            target.scraper = scraper
            target.mark_idle()
            self._idle_event.set()
            logger.info(
                "showheadless: Slot#%d (%s) ✅ berjalan no-headless",
                target.slot_id, account_name,
            )
            return {
                "ok": True,
                "message": (
                    f"Akun '{account_name}' (Slot#{target.slot_id}) berhasil direstart dalam mode no-headless"
                    f" — browser menuju {settings_url}"
                ),
            }
        except Exception as e:
            target.mark_dead()
            self._no_headless_slot_ids.discard(target.slot_id)
            logger.error(
                "showheadless: Slot#%d (%s) ❌ gagal restart: %s",
                target.slot_id, account_name, e, exc_info=True,
            )
            return {
                "ok": False,
                "message": f"Gagal restart akun '{account_name}': {e}",
            }

    async def stop_all_no_headless(self) -> dict:
        """
        Restart semua slot yang sedang berjalan dalam mode no-headless,
        kembalikan ke mode headless normal (sesuai self.headless).

        Slot yang BUSY di-skip dan dilaporkan.

        Returns dict {"ok": bool, "restarted": list, "skipped": list, "message": str}.
        """
        restarted: list[str] = []
        skipped: list[str]   = []

        targets = [
            slot for slot in self._slots
            if slot.slot_id in self._no_headless_slot_ids
        ]

        if not targets:
            return {
                "ok": True,
                "restarted": [],
                "skipped"  : [],
                "message"  : "Tidak ada slot yang berjalan dalam mode no-headless",
            }

        for slot in targets:
            account_name = slot.cookie_file.stem
            if slot.status == SlotStatus.BUSY:
                skipped.append(account_name)
                logger.warning(
                    "showheadlessstop: Slot#%d (%s) BUSY – skip",
                    slot.slot_id, account_name,
                )
                continue

            logger.info(
                "showheadlessstop: restart Slot#%d (%s) → headless=%s …",
                slot.slot_id, account_name, self.headless,
            )

            # Hapus dari set no-headless dulu sebelum restart
            self._no_headless_slot_ids.discard(slot.slot_id)

            if slot.scraper:
                try:
                    await slot.scraper.close_browser()
                except Exception:
                    pass
                slot.scraper = None
            slot.mark_dead()

            # Respawn normal (pakai self.headless)
            await self._init_slot(slot)
            restarted.append(account_name)

        parts = []
        if restarted:
            parts.append(f"Restarted: {', '.join(restarted)}")
        if skipped:
            parts.append(f"Skipped (busy): {', '.join(skipped)}")

        return {
            "ok"       : True,
            "restarted": restarted,
            "skipped"  : skipped,
            "message"  : " | ".join(parts) if parts else "Selesai",
        }

    def __repr__(self) -> str:
        s = self.status_summary()
        return (
            f"<BrowserPool total={s['total']} "
            f"idle={s['idle']} busy={s['busy']} "
            f"dead={s['dead']}>"
        )