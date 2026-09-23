#!/usr/bin/env python3
"""
public_chatgpt.py — Local Worker for the ChatGPT backend, mirroring
public_deepseek.py's Session persistence, envelope shape, and interactive
CLI (per design_chatgpt_backend.md / implementation.md, Tahap E).

Deviations from public_deepseek.py (v1 scope):
  • NO model_tab / deep_think / web_search / tool_messages (chat-only, no
    think_mode — see config/chatgpt.py).
  • NO --session-ttl CLI flag — sessions have no automatic TTL at all
    (mirrors the CHANGELOG "sesi 5" TTL-removal decision that already
    applies to deepseek/qwen). cleanup_older_than() is kept as a manual,
    explicit console command / hook only.
  • Registers with the VPS using "backend": "chatgpt" and the DeepSeek-style
    task envelope {"type":"task","task_id","request":{...}} (a brand-new
    worker, no legacy protocol to keep compatible with).

Usage
-----
  python public.py --backend chatgpt --vps ws://VPS_IP:8000/ws/worker \\
      --workers 2 --token MY_SHARED_SECRET

Commands (interactive REPL):
  list accounts     - Show all accounts
  add account NAME  - Add account runtime (auto-login)
  status            - Show pool status
  cleanup sessions [max_age_s] - Manually remove sessions unused for longer
  quit              - Graceful shutdown
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from browser_pool_chatgpt import BrowserPool
from config import DATA_SESSION_DIR
from scrapers.utils import get_logger

log = get_logger("paf_chatgpt.worker")

_SESSION_STORAGE_DIR = Path(DATA_SESSION_DIR) / "chatgpt"


# =========================================================================== #
# Session dataclass — NO is_expired()/TTL (per CHANGELOG "sesi 5" decision:
# sessions live until explicitly deleted, same as deepseek/qwen today).
# =========================================================================== #
@dataclass
class Session:
    session_id: str
    account: Optional[str] = None
    conversation_url: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    turn_count: int = 0

    def touch(self) -> None:
        self.last_used = time.time()

    def is_expired(self, max_age: float) -> bool:
        """Used ONLY by the manual `cleanup sessions [max_age_s]` command —
        never invoked automatically."""
        return (time.time() - self.last_used) > max_age


# =========================================================================== #
# SessionStore — persist dataSession/chatgpt/*.json
# =========================================================================== #
class SessionStore:
    def __init__(self, storage_dir: Path = _SESSION_STORAGE_DIR) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}

    def _path(self, session_id: str) -> Path:
        return self.storage_dir / f"{session_id}.json"

    def _to_dict(self, s: Session) -> dict:
        return {
            "session_id": s.session_id,
            "account": s.account,
            "conversation_url": s.conversation_url,
            "created_at": s.created_at,
            "last_used": s.last_used,
            "turn_count": s.turn_count,
        }

    def _from_dict(self, d: dict) -> Session:
        return Session(
            session_id=d["session_id"],
            account=d.get("account"),
            conversation_url=d.get("conversation_url"),
            created_at=d.get("created_at", time.time()),
            last_used=d.get("last_used", time.time()),
            turn_count=d.get("turn_count", 0),
        )

    def _save_to_disk(self, s: Session) -> None:
        try:
            self._path(s.session_id).write_text(
                json.dumps(self._to_dict(s), indent=2), encoding="utf-8",
            )
        except Exception as exc:
            log.warning("SessionStore: failed to save %s: %s", s.session_id[:8], exc)

    def _delete_from_disk(self, session_id: str) -> None:
        try:
            self._path(session_id).unlink(missing_ok=True)
        except Exception as exc:
            log.warning("SessionStore: failed to delete %s: %s", session_id[:8], exc)

    def load_from_disk(self) -> int:
        """Restore ALL persisted sessions unconditionally — no TTL check
        (mirrors public_deepseek.SessionStore.load_from_disk)."""
        restored = 0
        for path in self.storage_dir.glob("*.json"):
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                s = self._from_dict(d)
                self._sessions[s.session_id] = s
                restored += 1
            except Exception as exc:
                log.warning("SessionStore: failed to read %s: %s", path.name, exc)
        if restored:
            log.info("SessionStore(chatgpt): restored %d session(s) from disk", restored)
        return restored

    def create(self, session_id: Optional[str] = None, account: Optional[str] = None) -> Session:
        sid = session_id or uuid.uuid4().hex
        s = Session(session_id=sid, account=account)
        self._sessions[sid] = s
        self._save_to_disk(s)
        return s

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: Optional[str], account: Optional[str] = None) -> Session:
        if session_id:
            existing = self.get(session_id)
            if existing:
                return existing
        return self.create(session_id=session_id, account=account)

    def update(self, s: Session) -> None:
        self._sessions[s.session_id] = s
        self._save_to_disk(s)

    def bump_turn(self, session_id: str) -> None:
        s = self.get(session_id)
        if s:
            s.turn_count += 1
            s.touch()
            self.update(s)

    def delete(self, session_id: str) -> bool:
        existed = self._sessions.pop(session_id, None) is not None
        self._delete_from_disk(session_id)
        if existed:
            log.info("SessionStore(chatgpt): session %s deleted", session_id[:8])
        return existed

    def cleanup_older_than(self, max_age: float) -> int:
        old = [sid for sid, s in self._sessions.items() if s.is_expired(max_age)]
        for sid in old:
            del self._sessions[sid]
            self._delete_from_disk(sid)
        if old:
            log.info(
                "SessionStore(chatgpt): manually cleaned %d session(s) older than %.0fs",
                len(old), max_age,
            )
        return len(old)


# =========================================================================== #
# LocalWorker
# =========================================================================== #
class LocalWorker:
    BACKEND = "chatgpt"

    def __init__(
        self, vps_url: str, token: str, num_workers: int, headless: bool = True,
    ) -> None:
        self.vps_url = vps_url
        self.token = token
        self.num_workers = num_workers
        self.headless = headless
        self.worker_id: Optional[str] = None
        self.pool: Optional[BrowserPool] = None
        self._stop = asyncio.Event()
        self._ws = None
        self.session_store = SessionStore()
        self.session_store.load_from_disk()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_meta: dict[str, float] = {}
        self._keepalive_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        self.pool = BrowserPool(pool_size=self.num_workers, headless=self.headless)
        await self.pool.start()
        _s = self.pool.status_summary()
        log.info(
            "ChatGPT worker pool ready: %d idle, %d busy, %d dead (total %d)",
            _s["idle"], _s["busy"], _s["dead"], _s["total"],
        )

        threading.Thread(target=self._run_cli_loop, daemon=True).start()

        try:
            await self._connect_loop()
        finally:
            if self.pool:
                await self.pool.stop()

    async def _connect_loop(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                await self._serve()
                backoff = 1
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                log.warning("Connection lost (%s). Reconnecting in %ds...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception as exc:
                log.error("Unexpected worker error: %s", exc, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _serve(self) -> None:
        log.info("Connecting to VPS: %s", self.vps_url)
        async with websockets.connect(self.vps_url, max_size=None) as ws:
            self._ws = ws
            await ws.send(json.dumps({
                "type": "register",
                "backend": self.BACKEND,
                "token": self.token,
                "hostname": socket.gethostname(),
                "max_concurrent": self.num_workers,
                "accounts": [a["account"] for a in self.pool.list_accounts()] if self.pool else [],
            }))
            ack = json.loads(await ws.recv())
            if ack.get("type") != "registered":
                raise RuntimeError(f"Registration rejected: {ack}")
            self.worker_id = ack.get("worker_id")
            log.info("Registered with VPS as %s (backend=%s)", self.worker_id, self.BACKEND)

            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            await self._message_loop(ws)

    async def _message_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "task":
                asyncio.create_task(self._handle_task(ws, msg))
            elif mtype == "delete_session":
                asyncio.create_task(self._handle_delete_session(ws, msg))
            elif mtype == "ping":
                await ws.send(json.dumps({"type": "pong", "worker_id": self.worker_id}))

    async def _keepalive_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(30)
                if self._ws:
                    await self._ws.send(json.dumps({"type": "ping", "worker_id": self.worker_id}))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Per-session lock helpers (anti-collision CONTINUE)
    # ------------------------------------------------------------------ #
    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        self._session_locks_meta[session_id] = time.time()
        return self._session_locks[session_id]

    # ------------------------------------------------------------------ #
    # Session deletion (DELETE /v1/sessions/{session_id} — forwarded by VPS)
    # ------------------------------------------------------------------ #
    async def _handle_delete_session(self, ws, msg: dict) -> None:
        request_id = msg.get("request_id")
        session_id = msg.get("session_id")
        found = False
        try:
            if session_id:
                lock = await self._get_session_lock(session_id)
                async with lock:
                    found = self.session_store.delete(session_id)
            log.info("[%s] DELETE SESSION session=%s found=%s",
                     request_id, (session_id or "")[:8] or "-", found)
        except Exception as exc:
            log.error("[%s] DELETE SESSION error: %s", request_id, exc, exc_info=True)
        try:
            await ws.send(json.dumps({
                "type": "session_deleted",
                "request_id": request_id,
                "session_id": session_id,
                "found": found,
                "worker_id": self.worker_id,
            }))
        except Exception as exc:
            log.warning("[%s] Failed to send session_deleted reply: %s", request_id, exc)

    # ------------------------------------------------------------------ #
    # Task handling
    # ------------------------------------------------------------------ #
    async def _handle_task(self, ws, msg: dict) -> None:
        task_id = msg.get("task_id")
        request = msg.get("request", {})
        t_received = time.monotonic()
        prompt_preview = (request.get("prompt") or "")[:60].replace("\n", " ")
        mode = request.get("mode", "new")
        session_id = request.get("session_id")

        log.info("[%s] TASK RECEIVED | mode=%s session=%s prompt=%r",
                 task_id, mode, (session_id or "")[:8] or "-", prompt_preview)

        try:
            if request.get("stream"):
                await self._send_error(ws, task_id, "Streaming is not supported by this worker", status=400)
                return

            t_exec_start = time.monotonic()
            if session_id and mode == "continue":
                lock = await self._get_session_lock(session_id)
                async with lock:
                    result = await self._execute_task(request)
            else:
                result = await self._execute_task(request)
            t_exec_elapsed = time.monotonic() - t_exec_start
            t_total_elapsed = time.monotonic() - t_received

            if not result.get("ok"):
                error_msg = result.get("error", "Unknown error")
                status = result.pop("status", None) or self._map_error_to_status(error_msg)
                log.warning("[%s] TASK FAILED | elapsed=%.2fs | error=%s",
                            task_id, t_total_elapsed, str(error_msg)[:120])
                await self._send_error(ws, task_id, error_msg, status=status)
                return

            if session_id:
                conv_url = result.get("conversation_url")
                account = result.get("account")
                s = self.session_store.get_or_create(session_id, account=account)
                if conv_url:
                    s.conversation_url = conv_url
                if account and not s.account:
                    s.account = account
                s.touch()
                self.session_store.update(s)
                self.session_store.bump_turn(session_id)

            response_len = len(result.get("text") or "")
            log.info(
                "[%s] TASK DONE | total=%.2fs exec=%.2fs | account=%s mode=%s response_chars=%d",
                task_id, t_total_elapsed, t_exec_elapsed, result.get("account", "-"), mode, response_len,
            )

            await ws.send(json.dumps({
                "type": "result",
                "task_id": task_id,
                "result": result,
                "worker_id": self.worker_id,
            }))
        except Exception as exc:
            t_total_elapsed = time.monotonic() - t_received
            log.error("[%s] TASK ERROR | elapsed=%.2fs | %s", task_id, t_total_elapsed, exc, exc_info=True)
            await self._send_error(ws, task_id, str(exc), status=500)

    async def _execute_task(self, request: dict) -> dict:
        if not self.pool:
            return {"ok": False, "error": "Pool not initialized"}

        prompt = request.get("prompt", "")
        mode = request.get("mode", "new")
        session_id = request.get("session_id")
        preferred_account = request.get("preferred_account")

        attachments = None
        if request.get("attachments"):
            attachments = [
                {
                    "filename": a.get("filename", "file"),
                    "data": a.get("data", ""),
                    "mime_type": a.get("mime_type", "application/octet-stream"),
                }
                for a in request["attachments"]
            ]

        continue_url: Optional[str] = None
        if mode == "continue" and session_id:
            existing = self.session_store.get(session_id)
            if not existing or not existing.conversation_url:
                return {
                    "ok": False,
                    "error": "Session tidak ditemukan atau conversation_url kosong — "
                             "client harus membuat session baru",
                    "status": 404,
                }
            continue_url = existing.conversation_url
            if existing.account and not preferred_account:
                preferred_account = existing.account

        if preferred_account:
            available = [a["account"] for a in self.pool.list_accounts()]
            if preferred_account not in available:
                log.warning("Preferred account %s not available, using fallback", preferred_account)
                preferred_account = None

        try:
            result = await self.pool.run_task(
                prompt,
                mode=mode,
                attachments=attachments,
                continue_url=continue_url,
                preferred_account=preferred_account,
            )
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _send_error(self, ws, task_id: str, error: str, status: int = 500) -> None:
        await ws.send(json.dumps({
            "type": "error",
            "task_id": task_id,
            "error": error,
            "status": status,
            "worker_id": self.worker_id,
        }))

    def _map_error_to_status(self, error) -> int:
        error_lower = str(error).lower()
        if any(p in error_lower for p in ["rate limit", "usage cap", "too many requests"]):
            return 429
        if any(p in error_lower for p in ["timeout", "timed out"]):
            return 504
        if any(p in error_lower for p in ["not found", "404", "session tidak ditemukan"]):
            return 404
        if any(p in error_lower for p in ["unauthorized", "authentication", "login", "credentials"]):
            return 401
        return 500

    # ------------------------------------------------------------------ #
    # Interactive CLI (REPL)
    # ------------------------------------------------------------------ #
    def _run_cli_loop(self) -> None:
        print("\n" + "=" * 60)
        print("🎮 ChatGPT Worker Console")
        print("=" * 60)
        print("Commands:")
        print("  list accounts      - Show all accounts")
        print("  add account NAME   - Add account runtime (auto-login)")
        print("  status             - Show pool status")
        print("  cleanup sessions [max_age_s] - Manually remove old sessions")
        print("  quit               - Graceful shutdown")
        print("=" * 60 + "\n")

        while not self._stop.is_set():
            try:
                cmd = input("chatgpt-worker> ").strip()
                if not cmd:
                    continue
                if self._loop:
                    asyncio.run_coroutine_threadsafe(self._handle_command(cmd), self._loop)
            except (EOFError, KeyboardInterrupt):
                print("\nGraceful shutdown...")
                if self._loop:
                    asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
                break
            except Exception as exc:
                print(f"Error: {exc}")

    async def _handle_command(self, cmd: str) -> None:
        parts = cmd.split()
        if not parts:
            return
        command = parts[0].lower()

        if command == "list" and len(parts) == 2 and parts[1] == "accounts":
            if not self.pool:
                print("❌ Pool not initialized")
                return
            accounts = self.pool.list_accounts()
            print(f"\n📋 Accounts ({len(accounts)}):")
            for i, acc in enumerate(accounts, 1):
                print(f"  {i}. {acc['account']} (slot#{acc['slot_id']}, {acc['status']})")
            print()

        elif command == "add" and len(parts) == 3 and parts[1] == "account":
            await self._add_account_runtime(parts[2])

        elif command == "status":
            if not self.pool:
                print("❌ Pool not initialized")
                return
            _s = self.pool.status_summary()
            print(f"\n📊 Pool Status: {_s['idle']} idle, {_s['busy']} busy, "
                  f"{_s['dead']} dead (total {_s['total']})")
            print(f"   Worker ID: {self.worker_id}")
            print(f"   Connected: {self._ws is not None}\n")

        elif command == "cleanup" and len(parts) >= 2 and parts[1] == "sessions":
            max_age = 3600.0
            if len(parts) == 3:
                try:
                    max_age = float(parts[2])
                except ValueError:
                    print(f"❌ Invalid max_age_s: {parts[2]!r}")
                    return
            removed = self.session_store.cleanup_older_than(max_age)
            print(f"🧹 Cleanup: removed {removed} session(s) unused for more than {max_age:.0f}s")

        elif command == "quit":
            await self._shutdown()

        else:
            print(f"❌ Unknown command: {cmd}")

    async def _add_account_runtime(self, account_name: str) -> None:
        if not self.pool:
            print("❌ Pool not initialized")
            return
        try:
            print(f"➕ Adding account: {account_name}")
            self.pool.add_account(account_name)
            print(f"✅ Account {account_name} added (initializing in background)")
            await self._update_accounts_to_vps()
        except Exception as exc:
            print(f"❌ Failed to add account: {exc}")

    async def _update_accounts_to_vps(self) -> None:
        if not self._ws or not self.pool:
            return
        try:
            await self._ws.send(json.dumps({
                "type": "update_accounts",
                "worker_id": self.worker_id,
                "accounts": [a["account"] for a in self.pool.list_accounts()],
            }))
            log.info("Sent account update to VPS")
        except Exception as exc:
            log.warning("Failed to send account update: %s", exc)

    async def _shutdown(self) -> None:
        print("\n🛑 Shutting down ChatGPT worker...")
        self._stop.set()
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._ws:
            await self._ws.close()
        sys.exit(0)


# =========================================================================== #
# Main
# =========================================================================== #
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PAF-Model ChatGPT local worker")
    p.add_argument("--vps", required=True, help="VPS WebSocket URL (e.g., ws://VPS_IP:8000/ws/worker)")
    p.add_argument("--token", required=True, help="Shared secret token for authentication")
    p.add_argument("--workers", type=int, default=1, help="Number of concurrent browser slots (default 1)")
    p.add_argument("--headless", action="store_true", default=True, help="Run browsers headless (default True)")
    p.add_argument("--no-headless", action="store_true", help="Run browsers with visible UI (overrides --headless)")
    return p.parse_args(argv)


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    headless = not args.no_headless if args.no_headless else args.headless

    worker = LocalWorker(
        vps_url=args.vps, token=args.token, num_workers=args.workers, headless=headless,
    )
    try:
        await worker.start()
        return 0
    except KeyboardInterrupt:
        log.info("Interrupted")
        return 130
    except Exception as exc:
        log.error("Fatal: %s", exc, exc_info=True)
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
