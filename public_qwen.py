#!/usr/bin/env python3
"""
public.py – Local Worker dengan BrowserPool
============================================
Konek ke VPS via WebSocket, menerima task, menjalankan QwenScraper
dari pre-warmed BrowserPool, dan mengembalikan hasil ke VPS.

Perbedaan utama dari versi lama:
  • BrowserPool dibuat SEKALI saat startup → N browser sudah warm & login
  • Task tidak lagi spawn browser baru → tidak ada cold-start per task
  • Setiap slot dedicated ke 1 cookie file
  • Slot crash → auto-respawn di background

Flow:
  [public.py] → konek WebSocket → [vps_server.py]
  [vps_server.py]   → kirim task      → [public.py]
  [public.py] → ambil slot dari pool → send_prompt → kirim result → [vps_server.py]

Usage:
  python public.py --vps ws://YOUR_VPS_IP:9000/ws/worker --workers 20 --token YOUR_TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import websockets
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).parent))

from browser_pool_qwen import BrowserPool
from config import COOKIES_DIR, DATA_SESSION_DIR, PERSISTENT_CONTEXT_CONFIG
from scrapers.utils import setup_logger

logger = setup_logger("local_worker")


# ─── Session Store ─────────────────────────────────────────────────────────────

@dataclass
class Session:
    session_id: str
    cookie_file: Path                      # Path lengkap — dipakai untuk preferred_cookie di pool
    conversation_url: str | None = None
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    turn_count: int = 0

    def touch(self) -> None:
        self.last_used = time.time()
        self.turn_count += 1


class SessionStore:
    """
    In-memory session store dengan persistensi ke disk (dataSession/*.json).

    Setiap session disimpan sebagai file JSON tersendiri:
        dataSession/<session_id>.json

    Saat startup, semua file JSON di folder tersebut di-load kembali ke memory
    sehingga session yang belum expired tetap bisa dilanjutkan walau worker
    sempat dimatikan (Ctrl+C / crash / restart).

    File JSON ditulis ulang setiap kali session di-update (create / touch),
    dan dihapus dari disk saat session expired.
    """

    def __init__(self, ttl: int = 3600, store_dir: Path = DATA_SESSION_DIR) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.ttl = ttl
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)

    # ── Disk helpers ──────────────────────────────────────────────────────────

    def _session_path(self, session_id: str) -> Path:
        return self.store_dir / f"{session_id}.json"

    def _to_dict(self, s: Session) -> dict:
        return {
            "session_id":       s.session_id,
            "cookie_file":      str(s.cookie_file),
            "conversation_url": s.conversation_url,
            "created_at":       s.created_at,
            "last_used":        s.last_used,
            "turn_count":       s.turn_count,
        }

    def _from_dict(self, d: dict) -> Session:
        return Session(
            session_id=d["session_id"],
            cookie_file=Path(d["cookie_file"]),
            conversation_url=d.get("conversation_url"),
            created_at=d.get("created_at", time.time()),
            last_used=d.get("last_used", time.time()),
            turn_count=d.get("turn_count", 0),
        )

    def _save_to_disk(self, s: Session) -> None:
        """Tulis session ke disk (sync, dipanggil di dalam async lock)."""
        try:
            path = self._session_path(s.session_id)
            path.write_text(
                json.dumps(self._to_dict(s), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("SessionStore: gagal simpan session %s ke disk: %s", s.session_id[:8], e)

    def _delete_from_disk(self, session_id: str) -> None:
        """Hapus file session dari disk."""
        try:
            path = self._session_path(session_id)
            if path.exists():
                path.unlink()
        except Exception as e:
            logger.warning("SessionStore: gagal hapus file session %s: %s", session_id[:8], e)

    # ── Startup load ──────────────────────────────────────────────────────────

    def load_from_disk(self) -> int:
        """
        Load semua session dari disk ke memory saat startup.
        Session yang sudah expired langsung dihapus dari disk.
        Return: jumlah session yang berhasil di-restore.
        """
        now = time.time()
        restored = 0
        expired_files = []

        for path in self.store_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                s = self._from_dict(data)

                if now - s.last_used > self.ttl:
                    # Session sudah expired — hapus file
                    expired_files.append(path)
                    continue

                self._sessions[s.session_id] = s
                restored += 1
                logger.debug(
                    "SessionStore: restore session %s (cookie=%s, url=%s)",
                    s.session_id[:8],
                    s.cookie_file.name,
                    s.conversation_url or "-",
                )
            except Exception as e:
                logger.warning("SessionStore: gagal baca file session %s: %s", path.name, e)

        for path in expired_files:
            try:
                path.unlink()
            except Exception:
                pass

        if restored:
            logger.info(
                "SessionStore: %d session di-restore dari disk (%d expired dihapus)",
                restored, len(expired_files),
            )
        else:
            logger.info(
                "SessionStore: tidak ada session aktif di disk (%d expired dihapus)",
                len(expired_files),
            )

        return restored

    # ── Public API ────────────────────────────────────────────────────────────

    async def create(self, cookie_file: Path, session_id: str | None = None) -> Session:
        async with self._lock:
            sid = session_id or uuid.uuid4().hex
            session = Session(session_id=sid, cookie_file=cookie_file)
            self._sessions[sid] = session
            self._save_to_disk(session)
            return session

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            if time.time() - s.last_used > self.ttl:
                del self._sessions[session_id]
                self._delete_from_disk(session_id)
                return None
            return s

    async def get_or_create(self, session_id: str | None, cookie_file: Path) -> Session:
        if session_id:
            existing = await self.get(session_id)
            if existing:
                return existing
        return await self.create(cookie_file, session_id=session_id)

    async def update(self, s: Session) -> None:
        """Simpan ulang session ke disk setelah dimodifikasi (misal: touch, update URL)."""
        async with self._lock:
            self._sessions[s.session_id] = s
            self._save_to_disk(s)

    async def cleanup_expired(self) -> int:
        """Hapus semua session expired dari memory dan disk. Return jumlah yang dihapus."""
        async with self._lock:
            now = time.time()
            expired = [sid for sid, s in self._sessions.items()
                       if now - s.last_used > self.ttl]
            for sid in expired:
                del self._sessions[sid]
                self._delete_from_disk(sid)
            return len(expired)



# ─── Page Readiness Helper ─────────────────────────────────────────────────────

# Selector stop/cancel button yang muncul saat Qwen sedang memproses output.
# Selama tombol ini visible, halaman belum siap untuk menerima input baru.
_STOP_BTN_SELECTORS = [
    "button[aria-label='Stop']",
    "button[aria-label='Stop generating']",
    "button[data-testid='stop-button']",
    # fallback: cari button yang mengandung teks stop
    "button.stop-btn",
    ".generation-stop-btn",
]

# Selector input field — sama dengan kandidat di QwenScraper._find_input()
_INPUT_SELECTORS = [
    "textarea[placeholder]",
    "textarea#chat-input",
    "textarea[data-testid='chat-input']",
    "div[contenteditable='true'][data-testid]",
    "div[contenteditable='true']",
    "textarea",
]

_PAGE_READY_TIMEOUT = 60.0   # detik maks menunggu halaman siap
_PAGE_READY_POLL   = 0.5    # interval polling


async def _wait_page_ready(page, worker_label: str = "?") -> None:
    """
    Tunggu sampai halaman conversation Qwen benar-benar siap menerima input:

    Langkah 1 — Tunggu stop/cancel button HILANG dari halaman.
                 Selama Qwen masih memproses output sebelumnya, tombol ini visible.
                 Timeout: _PAGE_READY_TIMEOUT detik.

    Langkah 2 — Tunggu input field muncul dan visible.
                 Pastikan React component sudah mount dan input siap diketik.
                 Timeout: _PAGE_READY_TIMEOUT detik.

    Kedua langkah menggunakan polling ringan dengan asyncio.sleep
    agar tidak memblokir event loop.
    """
    deadline = asyncio.get_event_loop().time() + _PAGE_READY_TIMEOUT

    # ── Langkah 1: tunggu stop button hilang ──────────────────────────────────
    logger.debug("Worker#%s Menunggu Qwen selesai memproses...", worker_label)
    while asyncio.get_event_loop().time() < deadline:
        stop_visible = False
        for sel in _STOP_BTN_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    stop_visible = True
                    break
            except Exception:
                continue
        if not stop_visible:
            break
        await asyncio.sleep(_PAGE_READY_POLL)
    else:
        logger.warning(
            "Worker#%s Stop button masih visible setelah %.0fs — lanjut anyway",
            worker_label, _PAGE_READY_TIMEOUT,
        )

    # ── Langkah 2: tunggu input field visible ─────────────────────────────────
    logger.debug("Worker#%s Menunggu input field muncul...", worker_label)
    deadline2 = asyncio.get_event_loop().time() + _PAGE_READY_TIMEOUT
    while asyncio.get_event_loop().time() < deadline2:
        for sel in _INPUT_SELECTORS:
            try:
                el = await page.wait_for_selector(sel, timeout=1_000, state="visible")
                if el:
                    logger.debug(
                        "Worker#%s Input field siap (%s)", worker_label, sel
                    )
                    return   # ✅ halaman siap
            except Exception:
                continue
        await asyncio.sleep(_PAGE_READY_POLL)

    # Kalau sampai sini, input field tidak ditemukan — biarkan scraper yang handle error
    logger.warning(
        "Worker#%s Input field tidak ditemukan setelah %.0fs — scraper akan retry sendiri",
        worker_label, _PAGE_READY_TIMEOUT,
    )


# ─── Task Processor ────────────────────────────────────────────────────────────

class TaskProcessor:
    """
    Memproses satu task menggunakan slot dari BrowserPool.
    Tidak ada spawn browser di sini — semuanya diambil dari pool.
    """

    def __init__(self, pool: BrowserPool, session_ttl: int = 3600) -> None:
        self.pool = pool
        self.sessions = SessionStore(ttl=session_ttl, store_dir=DATA_SESSION_DIR)
        # Load session yang tersimpan dari run sebelumnya
        self.sessions.load_from_disk()
        # Per-session lock untuk task CONTINUE agar tidak tumpang tindih
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_meta: dict[str, float] = {}
        self._locks_mutex = asyncio.Lock()

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_mutex:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = asyncio.Lock()
            self._session_locks_meta[session_id] = time.time()
            return self._session_locks[session_id]

    async def _cleanup_session_locks(self, ttl: float = 3600.0) -> None:
        async with self._locks_mutex:
            now = time.time()
            stale = [
                sid for sid, ts in self._session_locks_meta.items()
                if now - ts > ttl and not self._session_locks[sid].locked()
            ]
            for sid in stale:
                del self._session_locks[sid]
                del self._session_locks_meta[sid]

    async def process(self, request_id: str, payload: dict, worker_label: str = "?") -> dict:
        """
        Proses satu task dari VPS.
        Task NEW     → acquire slot idle mana saja dari pool.
        Task CONTINUE → acquire slot dengan cookie yang SAMA dengan session awal,
                        lalu navigate ke conv_url sebelum send_prompt.
        """
        messages        = payload.get("messages", [])
        think_mode      = payload.get("think_mode")
        max_tokens      = payload.get("max_tokens")
        incoming_sid    = payload.get("session_id")
        raw_attachments: list[dict] = payload.get("attachments") or []
        payload_preferred_cookie: str | None = payload.get("preferred_cookie")

        # ── Task type support (create_image / create_video / web_search) ──
        task_type: str = payload.get("task_type", "chat")

        # ── Tool calling support ──────────────────────────────────────────────
        # tools: list of {type, function:{name, description, parameters}}
        tools: list[dict] | None = payload.get("tools")

        # Pisah messages berdasarkan role untuk routing logic
        user_msgs  = [m["content"] for m in messages if m.get("role") == "user" and m.get("content")]
        tool_msgs  = [m for m in messages if m.get("role") == "tool"]   # hasil eksekusi MCP
        prompt     = user_msgs[-1] if user_msgs else ""

        # Saat CONTINUE dengan tool_msgs, prompt boleh kosong
        # (Qwen cukup terima tool result + "continue")
        if not prompt and not tool_msgs:
            return {"success": False, "error": "Prompt kosong"}

        # ── Parse attachments dari payload ────────────────────────────────────
        # Konversi list of dict → list of Attachment objects
        attachments = []
        if raw_attachments:
            from scrapers.qwen_scraper import Attachment  # local import untuk hindari circular
            for raw in raw_attachments:
                try:
                    # Dukung dua sumber: base64 dari API, atau path lokal
                    if "data" in raw and raw["data"]:
                        att = Attachment.from_base64(
                            b64_data=raw["data"],
                            filename=raw.get("filename", "attachment"),
                            mime_type=raw.get("mime_type"),
                        )
                    elif "path" in raw and raw["path"]:
                        att = Attachment.from_path(raw["path"])
                    else:
                        logger.warning(
                            "Worker#%s Attachment dilewati (tidak ada data/path): %s",
                            worker_label, raw.get("filename"),
                        )
                        continue
                    attachments.append(att)
                    logger.debug(
                        "Worker#%s Attachment parsed: %s", worker_label, att,
                    )
                except Exception as e:
                    logger.warning(
                        "Worker#%s Gagal parse attachment '%s': %s",
                        worker_label, raw.get("filename"), e,
                    )

        if attachments:
            logger.info(
                "Worker#%s [%s] %d attachment(s): %s",
                worker_label, request_id[:8], len(attachments),
                [a.name for a in attachments],
            )

        # ── Resolve session ────────────────────────────────────────────────────
        mode = "new"
        conv_url: str | None = None
        preferred_cookie: str | None = None   # nama file, untuk pool.acquire()
        session_cookie_file: Path | None = None  # Path lengkap, untuk SessionStore

        if incoming_sid:
            existing = await self.sessions.get(incoming_sid)
            if existing:
                mode = "continue"
                conv_url = existing.conversation_url
                # ← FIX Bug #1 & #3: ambil cookie Path dari session, teruskan ke pool
                session_cookie_file = existing.cookie_file
                preferred_cookie    = existing.cookie_file.name
                logger.info(
                    "Worker#%s CONTINUE [%s] session=%s cookie=%s conv_url=%s",
                    worker_label, request_id[:8], incoming_sid[:8],
                    preferred_cookie, conv_url,
                )
            else:
                logger.info(
                    "Worker#%s Session tidak ditemukan/expired [%s] → mode new",
                    worker_label, request_id[:8],
                )

        if mode == "new":
            # Jika client memilih akun spesifik via field `model` (misal "account6"),
            # VPS mengirimnya sebagai preferred_cookie.
            # Pool mencocokkan dengan cookie_file.name (misal "account6.json"),
            # jadi tambahkan ekstensi jika belum ada.
            if payload_preferred_cookie:
                pc = payload_preferred_cookie
                if not pc.endswith(".json"):
                    pc = pc + ".json"
                preferred_cookie = pc
                logger.info(
                    "Worker#%s NEW [%s] cookie hint dari model: %s",
                    worker_label, request_id[:8], preferred_cookie,
                )
            else:
                logger.info("Worker#%s NEW [%s]", worker_label, request_id[:8])

        # Per-session lock hanya untuk CONTINUE (agar 2 request ke session yg sama tidak tabrakan)
        lock: asyncio.Lock | None = None
        if mode == "continue" and incoming_sid:
            lock = await self._get_session_lock(incoming_sid)

        async def _run() -> dict:
            logger.debug(
                "Worker#%s Pool status: %s", worker_label, self.pool.status_summary(),
            )

            # ← FIX Bug #3: teruskan preferred_cookie ke acquire()
            async with self.pool.acquire(preferred_cookie=preferred_cookie) as (scraper, cookie_name, _slot_id):
                try:
                    # Untuk CONTINUE: navigasi ke URL conversation yang tersimpan,
                    # HANYA jika browser belum berada di halaman tersebut.
                    # Skip goto() jika current URL sudah sama → hemat 2–6 detik load time.
                    if mode == "continue" and conv_url and "chat.qwen.ai" in conv_url:
                        current_url_now = scraper._page.url
                        already_there = (
                            conv_url in current_url_now
                            or current_url_now in conv_url
                        )
                        if already_there:
                            logger.info(
                                "Worker#%s Skip goto() — browser sudah di halaman: %s",
                                worker_label, conv_url,
                            )
                        else:
                            logger.info(
                                "Worker#%s Navigasi ke conversation: %s",
                                worker_label, conv_url,
                            )
                            await scraper._page.goto(
                                conv_url, wait_until="domcontentloaded", timeout=30_000
                            )
                            # Tunggu halaman benar-benar siap sebelum scrape():
                            # 1. Tunggu Qwen selesai memproses (stop button hilang)
                            # 2. Tunggu input field visible dan enabled
                            await _wait_page_ready(scraper._page, worker_label)
                        scraper._conversation_started = True

                    # Override think_mode per-request jika dikirim dari VPS
                    if think_mode:
                        scraper._think_mode = think_mode
                        scraper._think_mode_applied = False

                    # Teruskan max_tokens per-request
                    scraper._max_tokens = max_tokens

                    # ── Tool calling: inject tools ke scraper ─────────────────
                    scraper._tools = tools  # None jika request tidak punya tools

                    # ── Routing berdasarkan task_type + mode + tool_msgs ─────────
                    if task_type == "create_image":
                        # Media task: create image via Qwen "Create Image" button
                        logger.info(
                            "Worker#%s CREATE_IMAGE [%s] prompt='%s'",
                            worker_label, request_id[:8], prompt[:60],
                        )
                        result = await scraper.create_image(prompt)
                    elif task_type == "create_video":
                        # Media task: create video via Qwen "Create Video" button
                        logger.info(
                            "Worker#%s CREATE_VIDEO [%s] prompt='%s'",
                            worker_label, request_id[:8], prompt[:60],
                        )
                        result = await scraper.create_video(prompt)
                    elif task_type == "web_search":
                        # Web search: use Qwen "Web search" button
                        logger.info(
                            "Worker#%s WEB_SEARCH [%s] prompt='%s'",
                            worker_label, request_id[:8], prompt[:60],
                        )
                        result = await scraper.web_search(prompt)
                    elif mode == "continue" and tool_msgs:
                        # TURN 2: CLI mengembalikan hasil eksekusi tool via MCP.
                        last_tool_idx = max(
                            i for i, m in enumerate(messages) if m.get("role") == "tool"
                        )
                        msgs_after_tools = messages[last_tool_idx + 1:]
                        next_user = next(
                            (m["content"] for m in msgs_after_tools if m.get("role") == "user" and m.get("content")),
                            None,
                        )
                        logger.info(
                            "Worker#%s TOOL_RESULT [%s] tools=%d next_user=%s",
                            worker_label, request_id[:8],
                            len(tool_msgs),
                            repr(next_user[:40]) if next_user else "None",
                        )
                        result = await scraper.scrape_with_tool_result(tool_msgs, next_user)
                    else:
                        # TURN 1 (NEW) atau CONTINUE biasa tanpa tool result (chat)
                        result = await scraper.scrape(prompt, mode=mode, attachments=attachments or None)

                    current_url: str = scraper._page.url

                except asyncio.TimeoutError:
                    return {"success": False, "error": "Timeout: Qwen AI tidak merespons"}
                except Exception as e:
                    logger.error(
                        "Worker#%s Scraper error [%s]: %s",
                        worker_label, request_id[:8], e, exc_info=True,
                    )
                    raise  # biarkan pool.acquire() tangkap dan mark dead

            if not result.get("success"):
                return {"success": False, "error": result.get("error", "Unknown scraper error")}

            # Resolusi cookie_file sebagai Path untuk disimpan di session
            if session_cookie_file:
                resolved_cookie_path = session_cookie_file
            else:
                resolved_cookie_path = self.pool.get_cookie_path(cookie_name)

            session = await self.sessions.get_or_create(incoming_sid, resolved_cookie_path)
            if current_url and "chat.qwen.ai" in current_url:
                session.conversation_url = current_url
            session.touch()
            await self.sessions.update(session)

            # ── Map internal result → OpenAI-compatible response ─────────────
            ts           = int(time.time())
            account_stem = cookie_name.replace(".json", "") if cookie_name else "qwen"
            openai_id    = f"chatcmpl-qwen-{ts}-{account_stem}"
            x_metadata   = result.get("x_metadata", {})
            usage        = result.get("usage", {})
            finish_reason = result.get("finish_reason", "stop")

            if finish_reason == "tool_calls":
                # Qwen meminta pemanggilan tool → return tool_calls ke VPS/CLI
                tool_calls_list = result.get("tool_calls", [])
                logger.info(
                    "Worker#%s TOOL_CALLS [%s] %d call(s) | session=%s",
                    worker_label, request_id[:8], len(tool_calls_list),
                    session.session_id[:8],
                )
                openai_response = {
                    "success": True,
                    "finish_reason": "tool_calls",
                    "tool_calls": tool_calls_list,
                    "session_id": session.session_id,
                    "cookie_file": cookie_name,
                    "conversation_url": session.conversation_url or "",
                    "usage": usage,
                    "x_metadata": x_metadata,
                }
            else:
                # Jawaban final (stop)
                response_text: str = result.get("response", "")
                logger.info(
                    "Worker#%s ✅ [%s] %d chars | session=%s | cookie=%s | url=%s",
                    worker_label, request_id[:8], len(response_text),
                    session.session_id[:8], cookie_name,
                    session.conversation_url or "-",
                )
                # Extract media URLs for create_image/create_video tasks
                media_urls = result.get("urls", [])

                openai_response = {
                    "success": True,
                    "finish_reason": "stop",
                    "response": response_text,
                    "urls": media_urls,
                    "task_type": task_type,
                    "session_id": session.session_id,
                    "cookie_file": cookie_name,
                    "conversation_url": session.conversation_url or "",
                    "account_used": cookie_name,
                    "usage": usage,
                    "x_metadata": x_metadata,
                }
            return openai_response
            # ────────────────────────────────────────────────────────────────────────

        if lock:
            async with lock:
                return await _run()
        else:
            return await _run()


# ─── WebSocket Worker ──────────────────────────────────────────────────────────

class LocalWorker:
    """
    Konek ke VPS via WebSocket dan delegasikan task ke TaskProcessor.
    Concurrency diatur oleh BrowserPool (jumlah slot idle).
    """

    _instance_counter: int = 0

    def __init__(
        self,
        vps_url: str,
        processor: TaskProcessor,
        max_concurrent: int = 4,
        token: str | None = None,
        reconnect_delay: float = 5.0,
    ) -> None:
        self.vps_url = vps_url
        self.processor = processor
        self.max_concurrent = max_concurrent
        self.token = token
        self.reconnect_delay = reconnect_delay
        self._running = True
        self._label = str(LocalWorker._instance_counter)
        LocalWorker._instance_counter += 1

    def _build_url(self) -> str:
        if self.token:
            sep = "&" if "?" in self.vps_url else "?"
            return f"{self.vps_url}{sep}token={self.token}"
        return self.vps_url

    async def _handle_task(self, ws, request_id: str, payload: dict) -> None:
        logger.info("Worker#%s ▶ Request [%s]", self._label, request_id[:8])

        # ── Request Forwarding: validasi inbound ─────────────────────────────
        # Reject stream:true sebelum masuk processor
        if payload.get("stream") is True:
            error_msg = {
                "type": "result",
                "request_id": request_id,
                "data": {
                    "success": False,
                    "error": {
                        "type": "unsupported_feature",
                        "message": "Streaming is not supported. Send stream: false or omit the field.",
                    },
                },
            }
            try:
                await ws.send(json.dumps(error_msg))
            except Exception as e:
                logger.error("Worker#%s Gagal kirim stream-reject ke VPS: %s", self._label, e)
            return

        # Validasi required fields
        messages = payload.get("messages", [])
        if not messages:
            error_msg = {
                "type": "result",
                "request_id": request_id,
                "data": {
                    "success": False,
                    "error": {
                        "type": "validation_error",
                        "message": "Field 'messages' wajib ada dan tidak boleh kosong.",
                    },
                },
            }
            try:
                await ws.send(json.dumps(error_msg))
            except Exception as e:
                logger.error("Worker#%s Gagal kirim validation-error ke VPS: %s", self._label, e)
            return

        # Field mapping: model → preferred_cookie (jika belum di-set VPS)
        if payload.get("model") and not payload.get("preferred_cookie"):
            payload["preferred_cookie"] = payload["model"]

        # Pisah system message dari messages
        # CATATAN: tools tetap ada di payload["tools"], tidak disentuh di sini
        system_messages = [m for m in messages if m.get("role") == "system"]
        non_system_messages = [m for m in messages if m.get("role") != "system"]

        if system_messages:
            # Jika tools ada, system_prompt akan di-merge ke [SYSTEM CONTEXT] oleh scraper
            payload["system_prompt"] = "\n".join(
                m["content"] for m in system_messages if m.get("content")
            )
            payload["messages"] = non_system_messages

        # conversation_history: semua pesan kecuali yang terakhir (hanya untuk mode CONTINUE)
        incoming_sid = payload.get("session_id")
        user_msgs = [m for m in payload.get("messages", []) if m.get("role") == "user"]
        if incoming_sid and len(user_msgs) > 1:
            payload["conversation_history"] = payload["messages"][:-1]

        # Log tools count untuk debugging
        tools = payload.get("tools")
        if tools:
            logger.info("Worker#%s Tools available: %d", self._label, len(tools))
        # ─────────────────────────────────────────────────────────────────────

        try:
            result = await self.processor.process(request_id, payload, worker_label=self._label)
            msg = {"type": "result", "request_id": request_id, "data": result}
        except Exception as e:
            logger.error("Worker#%s Error [%s]: %s", self._label, request_id[:8], e)
            msg = {"type": "error", "request_id": request_id, "message": str(e)}

        # ── Error forwarding: map error.type → HTTP status code ──────────────
        # Tempel http_status di dalam data agar VPS bisa forward ke client
        _ERROR_STATUS_MAP = {
            "validation_error":    422,
            "unsupported_feature": 400,
            "rate_limited":        429,
            "expired":             503,
            "banned":              503,
            "unknown":             500,
        }
        if msg.get("type") == "result":
            data = msg.get("data", {})
            if not data.get("success"):
                err = data.get("error")
                if isinstance(err, dict):
                    err_type = err.get("type", "unknown")
                    err["http_status"] = _ERROR_STATUS_MAP.get(err_type, 500)
                    # Format error response OpenAI-compatible
                    data["error_response"] = {
                        "error": {
                            "message": err.get("message", "Internal error"),
                            "type": err_type,
                            "code": err_type,
                        }
                    }
                elif isinstance(err, str):
                    # Error string lama (dari scraper) → deteksi tipe otomatis
                    err_lower = err.lower()
                    if "rate" in err_lower or "quota" in err_lower:
                        detected = "rate_limited"
                    elif "expired" in err_lower or "session" in err_lower:
                        detected = "expired"
                    elif "banned" in err_lower:
                        detected = "banned"
                    elif "stream" in err_lower:
                        detected = "unsupported_feature"
                    elif "prompt" in err_lower or "field" in err_lower or "kosong" in err_lower:
                        detected = "validation_error"
                    else:
                        detected = "unknown"
                    data["error"] = {
                        "type": detected,
                        "message": err,
                        "http_status": _ERROR_STATUS_MAP.get(detected, 500),
                    }
                    data["error_response"] = {
                        "error": {
                            "message": err,
                            "type": detected,
                            "code": detected,
                        }
                    }
        # ─────────────────────────────────────────────────────────────────────

        try:
            await ws.send(json.dumps(msg))
        except Exception as e:
            logger.error("Worker#%s Gagal kirim result ke VPS: %s", self._label, e)

    async def _handle_command(self, ws, command_id: str, command: str, args: dict) -> None:
        """
        Proses perintah admin yang dikirim VPS.

        Perintah yang didukung:
          listaccounts       → daftar semua akun + status
          busyaccounts       → daftar akun yang sedang BUSY
          showheadless       → restart satu akun dalam mode no-headless
                               args: {"account": "<namaAccount>"}
          showheadlessstop   → restart semua slot no-headless kembali ke mode normal
        """
        pool = self.processor.pool
        cmd  = command.strip().lower()
        result: dict

        try:
            if cmd == "listaccounts":
                accounts = pool.list_accounts()
                result = {
                    "ok"      : True,
                    "command" : command,
                    "accounts": accounts,
                    "total"   : len(accounts),
                }
                logger.info(
                    "Command [listaccounts]: %d akun – %s",
                    len(accounts),
                    [a["account"] + "(" + a["status"] + ")" for a in accounts],
                )

            elif cmd == "busyaccounts":
                busy = pool.busy_accounts()
                result = {
                    "ok"      : True,
                    "command" : command,
                    "accounts": busy,
                    "total"   : len(busy),
                }
                logger.info(
                    "Command [busyaccounts]: %d akun busy – %s",
                    len(busy),
                    [a["account"] for a in busy],
                )

            elif cmd == "showheadless":
                account_name = args.get("account", "").strip()
                if not account_name:
                    result = {
                        "ok"     : False,
                        "command": command,
                        "message": "Harap sertakan nama akun. Contoh: showheadless account1",
                    }
                else:
                    logger.info("Command [showheadless]: target akun = '%s'", account_name)
                    result = await pool.restart_slot_no_headless(account_name)
                    result["command"] = command

            elif cmd == "showheadlessstop":
                logger.info("Command [showheadlessstop]: menghentikan semua slot no-headless …")
                result = await pool.stop_all_no_headless()
                result["command"] = command

            else:
                result = {
                    "ok"     : False,
                    "command": command,
                    "message": (
                        f"Perintah tidak dikenal: '{command}'. "
                        "Perintah yang tersedia: listaccounts | busyaccounts | "
                        "showheadless <namaAccount> | showheadlessstop"
                    ),
                }
                logger.warning("Command tidak dikenal: '%s'", command)

        except Exception as e:
            logger.error("Error saat menjalankan command '%s': %s", command, e, exc_info=True)
            result = {
                "ok"     : False,
                "command": command,
                "message": f"Internal error: {e}",
            }

        try:
            await ws.send(json.dumps({
                "type"      : "command_result",
                "command_id": command_id,
                "data"      : result,
            }))
        except Exception as e:
            logger.error("Gagal kirim command_result ke VPS: %s", e)

    async def _send_accounts_when_ready(self, ws) -> None:
        """
        Tunggu sampai pool punya minimal 1 slot aktif, lalu kirim update_accounts ke VPS.
        Ini menangani kasus di mana register dikirim sebelum pool selesai warm-up,
        sehingga accounts[] kosong pada pesan register awal.
        """
        for _ in range(120):  # tunggu max 120 detik
            accounts = self.processor.pool.list_accounts()
            if accounts:
                try:
                    await ws.send(json.dumps({
                        "type": "update_accounts",
                        "accounts": accounts,
                    }))
                    logger.info(
                        "Worker#%s 📋 Pool siap — dilaporkan %d akun ke VPS: %s",
                        self._label, len(accounts),
                        [a["account"] + "(" + a["status"] + ")" for a in accounts],
                    )
                except Exception as e:
                    logger.warning("Worker#%s gagal kirim update_accounts: %s", self._label, e)
                return
            await asyncio.sleep(1)
        logger.warning(
            "Worker#%s ⚠️ Pool masih kosong setelah 120 detik — akun tidak dilaporkan ke VPS.",
            self._label,
        )

    async def _keepalive(self, ws) -> None:
        """Kirim ping ke VPS setiap 30 detik supaya koneksi tidak putus."""
        while True:
            await asyncio.sleep(30)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception:
                break

    async def _status_reporter(self) -> None:
        """Log status pool setiap 60 detik."""
        while self._running:
            await asyncio.sleep(60)
            try:
                s = self.processor.pool.status_summary()
                logger.info(
                    "Pool status: total=%d idle=%d busy=%d dead=%d starting=%d",
                    s["total"], s["idle"], s["busy"], s["dead"], s["starting"],
                )
                # Cleanup expired sessions dan locks secara berkala
                cleaned = await self.processor.sessions.cleanup_expired()
                if cleaned:
                    logger.debug("Cleaned %d expired session(s)", cleaned)
                await self.processor._cleanup_session_locks()
            except Exception:
                pass

    async def _connect_and_run(self) -> None:
        url = self._build_url()
        logger.info("Worker#%s 🔌 Konek ke VPS: %s", self._label, self.vps_url)

        async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
            await ws.send(json.dumps({
                "type": "register",
                "backend": "qwen",   # ← model routing key (unified VPS)
                "max_concurrent": self.max_concurrent,
                "accounts": self.processor.pool.list_accounts(),  # mungkin [] saat pool belum siap
            }))
            logger.info(
                "Worker#%s ✅ Terhubung ke VPS! (max_concurrent=%d)",
                self._label, self.max_concurrent,
            )

            keepalive_task  = asyncio.create_task(self._keepalive(ws))
            status_task     = asyncio.create_task(self._status_reporter())
            accounts_task   = asyncio.create_task(self._send_accounts_when_ready(ws))

            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Worker#%s Pesan tidak valid dari VPS: %s",
                            self._label, raw[:100],
                        )
                        continue

                    msg_type = msg.get("type")

                    if msg_type == "task":
                        request_id = msg["request_id"]
                        payload    = msg["payload"]
                        logger.info(
                            "Worker#%s → Request [%s] session=%s",
                            self._label, request_id[:8],
                            (payload.get("session_id") or "new")[:8],
                        )
                        # Non-blocking: task jalan paralel di background
                        asyncio.create_task(self._handle_task(ws, request_id, payload))

                    elif msg_type == "pong":
                        logger.debug("Worker#%s Pong dari VPS", self._label)

                    elif msg_type == "command":
                        command_id = msg.get("command_id", "")
                        command    = msg.get("command", "")
                        cmd_args   = msg.get("args", {})
                        logger.info(
                            "Worker#%s ← Command [%s]: '%s' args=%s",
                            self._label, command_id[:8] if command_id else "-",
                            command, cmd_args,
                        )
                        asyncio.create_task(
                            self._handle_command(ws, command_id, command, cmd_args)
                        )

                    else:
                        logger.debug(
                            "Worker#%s Pesan tidak dikenal: %s",
                            self._label, msg_type,
                        )
            finally:
                keepalive_task.cancel()
                status_task.cancel()
                accounts_task.cancel()

    async def run(self) -> None:
        """Loop utama dengan auto-reconnect."""
        while self._running:
            try:
                await self._connect_and_run()
            except ConnectionClosed as e:
                logger.warning("❌ Koneksi ke VPS terputus: %s", e)
            except OSError as e:
                logger.warning("❌ Tidak bisa konek ke VPS: %s", e)
            except Exception as e:
                logger.error("❌ Error tidak terduga: %s", e, exc_info=True)

            if self._running:
                logger.info("🔄 Reconnect dalam %.0f detik...", self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    def stop(self) -> None:
        self._running = False


# ─── Console Command Loop ──────────────────────────────────────────────────────

CONSOLE_HELP = """
╔══════════════════════════════════════════════════════════╗
║              Perintah Console Worker                     ║
╠══════════════════════════════════════════════════════════╣
║  listaccounts             Tampilkan semua akun + status  ║
║  busyaccounts             Tampilkan akun yang BUSY       ║
║  addaccount <file>        Daftarkan akun baru dari file  ║
║                           cookie (tanpa/dengan .json)    ║
║  showheadless <account>   Restart akun dalam mode        ║
║                           no-headless (browser visible)  ║
║  showheadlessstop         Kembalikan semua no-headless   ║
║                           ke mode headless normal        ║
║  help                     Tampilkan bantuan ini          ║
╚══════════════════════════════════════════════════════════╝
"""


async def _run_console_command(pool: "BrowserPool", line: str) -> None:
    """
    Parse dan eksekusi satu baris perintah console langsung ke BrowserPool.
    Output dicetak ke stdout agar mudah dibaca di console.
    """
    parts = line.strip().split()
    if not parts:
        return

    cmd = parts[0].lower()

    # ── listaccounts ──────────────────────────────────────────────────────────
    if cmd == "listaccounts":
        accounts = pool.list_accounts()
        print()
        print(f"  {'ACCOUNT':<20} {'STATUS':<10} {'SLOT':>4}  {'NO-HEADLESS'}")
        print("  " + "-" * 50)
        for a in accounts:
            nh = "yes" if a["no_headless"] else ""
            print(f"  {a['account']:<20} {a['status']:<10} {a['slot_id']:>4}  {nh}")
        print(f"\n  Total: {len(accounts)} akun\n")

    # ── busyaccounts ──────────────────────────────────────────────────────────
    elif cmd == "busyaccounts":
        busy = pool.busy_accounts()
        print()
        if not busy:
            print("  Tidak ada akun yang sedang BUSY.\n")
        else:
            print(f"  {'ACCOUNT':<20} {'STATUS':<10} {'SLOT':>4}")
            print("  " + "-" * 36)
            for a in busy:
                print(f"  {a['account']:<20} {a['status']:<10} {a['slot_id']:>4}")
            print(f"\n  Total busy: {len(busy)} akun\n")

    # ── addaccount <file> ────────────────────────────────────────────────────
    elif cmd == "addaccount":
        if len(parts) < 2:
            print("\n  Harap sertakan nama file cookie. Contoh: addaccount account3.json\n")
            return
        cookie_filename = parts[1]
        print(f"\n  Mendaftarkan akun dari file '{cookie_filename}' ...")
        result = await pool.add_account(cookie_filename)
        ok = "OK" if result["ok"] else "GAGAL"
        slot_info = f" (Slot#{result['slot_id']})" if result["slot_id"] is not None else ""
        print(f"  [{ok}]{slot_info} {result['message']}\n")

    # ── showheadless <account> ────────────────────────────────────────────────
    elif cmd == "showheadless":
        if len(parts) < 2:
            print("\n  Harap sertakan nama akun. Contoh: showheadless account1\n")
            return
        account_name = parts[1]
        print(f"\n  Merestart '{account_name}' dalam mode no-headless ...")
        result = await pool.restart_slot_no_headless(account_name)
        ok = "OK" if result["ok"] else "GAGAL"
        print(f"  [{ok}] {result['message']}\n")

    # ── showheadlessstop ──────────────────────────────────────────────────────
    elif cmd == "showheadlessstop":
        print("\n  Menghentikan semua slot no-headless ...")
        result = await pool.stop_all_no_headless()
        ok = "OK" if result["ok"] else "GAGAL"
        print(f"  [{ok}] {result['message']}")
        if result.get("restarted"):
            print(f"  Restarted : {', '.join(result['restarted'])}")
        if result.get("skipped"):
            print(f"  Skipped   : {', '.join(result['skipped'])} (sedang busy)")
        print()

    # ── help ──────────────────────────────────────────────────────────────────
    elif cmd in ("help", "?"):
        print(CONSOLE_HELP)

    else:
        print(f"\n  Perintah tidak dikenal: '{line.strip()}'. Ketik 'help' untuk bantuan.\n")


async def _console_loop(pool: "BrowserPool", worker: "LocalWorker") -> None:
    """
    Baca perintah dari stdin secara async (tidak memblokir event loop).
    Berjalan paralel dengan worker.run() via asyncio.gather().
    """
    loop = asyncio.get_event_loop()
    print(CONSOLE_HELP)
    while worker._running:
        try:
            # input() blocking — jalankan di thread pool agar tidak blokir event loop
            line: str = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            break

        line = line.strip()
        if not line:
            continue

        try:
            await _run_console_command(pool, line)
        except Exception as e:
            print(f"\n  Error saat menjalankan perintah: {e}\n")


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="local-worker",
        description="Local Worker – BrowserPool + WebSocket ke VPS",
    )
    parser.add_argument(
        "--vps", required=True, metavar="URL",
        help="WebSocket URL VPS, contoh: ws://1.2.3.4:9000/ws/worker",
    )
    parser.add_argument(
        "--token", default=None,
        help="Token autentikasi (harus sama dengan --token di vps_server.py)",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Jumlah slot browser di pool (default: 4)",
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Tampilkan jendela browser",
    )
    parser.add_argument(
        "--cookies-dir", metavar="DIR", type=Path, default=COOKIES_DIR,
    )
    parser.add_argument(
        "--session-ttl", type=int, default=3600,
        help="Session TTL dalam detik (default: 3600)",
    )
    parser.add_argument(
        "--reconnect-delay", type=float, default=5.0,
        help="Jeda sebelum reconnect ke VPS (detik)",
    )
    parser.add_argument(
        "--think-mode", default=None,
        choices=["auto", "thinking", "fast"],
        help="Default think mode untuk semua slot (default: dari config)",
    )
    args = parser.parse_args()

    headless = not args.no_headless

    logger.info("=" * 58)
    logger.info("  Local Worker – AIChatScraper (BrowserPool)")
    logger.info("  VPS         : %s", args.vps)
    logger.info("  Pool size   : %d browser", args.workers)
    logger.info("  Headless    : %s", headless)
    logger.info("  Cookies dir : %s", args.cookies_dir)
    logger.info("  Think mode  : %s", args.think_mode or "dari config")
    logger.info("  Token       : %s", "✅ Aktif" if args.token else "❌ Tidak diset")
    logger.info("=" * 58)

    async def _main() -> None:
        # 1. Buat dan warm-up pool
        pool = BrowserPool(
            cookies_dir=args.cookies_dir,
            pool_size=args.workers,
            headless=headless,
            think_mode=args.think_mode,
        )
        try:
            logger.info("Warming up %d browser slot(s)...", args.workers)
            await pool.start()
            logger.info("Pool ready → %s", pool)
        except Exception as e:
            logger.critical("Gagal start BrowserPool: %s", e)
            sys.exit(1)

        # 2. Buat processor dan worker
        processor = TaskProcessor(pool=pool, session_ttl=args.session_ttl)
        worker = LocalWorker(
            vps_url=args.vps,
            processor=processor,
            max_concurrent=args.workers,
            token=args.token,
            reconnect_delay=args.reconnect_delay,
        )

        # 3. Jalankan worker + console loop paralel; tutup pool saat selesai
        try:
            await asyncio.gather(
                worker.run(),
                _console_loop(pool, worker),
            )
        finally:
            logger.info("Menutup BrowserPool...")
            await pool.stop()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Worker dihentikan.")


if __name__ == "__main__":
    main()