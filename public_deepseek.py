#!/usr/bin/env python3
"""
public.py — Enhanced Local Worker with Session Persistence & Interactive CLI.

Runs on a machine that is logged into DeepSeek (via cookies) and owns a
pre-warmed BrowserPool. Connects to the VPS over WebSocket, receives tasks,
runs them through the pool, and streams results back. Auto-reconnects.

NEW FEATURES:
  1. Session persistence (disk) via SessionStore  [upgraded: TTL, account pin,
     load_from_disk, cleanup_expired, bump_turn]
  2. CONTINUE: navigate to conversation URL + skip-goto optimisation
  3. Per-session lock (anti-collision CONTINUE) + lock TTL cleanup
  4. preferred_account routing — CONTINUE forces same account as Turn 1
  5. Keepalive ping to VPS (anti-disconnect)
  6. update_accounts to VPS when pool ready
  7. Console interactive loop (CLI commands)
  8. add_account runtime (no restart)
  9. showheadless (toggle browser visibility per account)
  10. Error forwarding + HTTP status mapping
  11. Stream: true rejection (unsupported)
  12. Attachment via Attachment class (base64)
  13. Auto-reconnect to VPS (enhanced backoff)
  14. Tool-result injection: scrape_with_tool_result() path for CONTINUE Turn 2
  15. Background cleanup task (expired sessions + idle locks every 60 s)

Usage
-----
  python public.py --vps ws://VPS_IP:8000/ws/worker \
      --workers 2 --token MY_SHARED_SECRET

Commands (interactive REPL):
  list accounts     - Show all accounts
  add account NAME  - Add account runtime (auto-login)
  status            - Show pool status
  showheadless ACC  - Toggle headless for account
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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from browser_pool_deepseek import BrowserPool
from config import DATA_SESSION_DIR, ROTATION_CONFIG
from scrapers.utils import get_logger

log = get_logger("paf_deepseek.worker")

_SESSION_TTL: int = ROTATION_CONFIG.get("session_ttl", 3600)


# =========================================================================== #
# Session dataclass
# =========================================================================== #
@dataclass
class Session:
    session_id: str
    account: Optional[str] = None          # account used for Turn 1 → pinned for CONTINUE
    conversation_url: Optional[str] = None  # URL of the live DeepSeek thread
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    turn_count: int = 0

    def touch(self) -> None:
        self.last_used = time.time()

    def is_expired(self, ttl: int) -> bool:
        return (time.time() - self.last_used) > ttl


# =========================================================================== #
# SessionStore (Disk Persistence) — upgraded from simple key→JSON to
# full Session objects with TTL, load-from-disk, and cleanup.
# =========================================================================== #
class SessionStore:
    """
    Persistent session store for CONTINUE mode.

    Each session is a JSON file under dataSession/<session_id>.json.
    Sessions expire after `ttl` seconds of inactivity and are removed from
    both memory and disk on next access or explicit cleanup_expired().
    On startup, load_from_disk() restores all non-expired sessions so that
    conversations survive a worker restart.
    """

    def __init__(
        self,
        ttl: int = _SESSION_TTL,
        storage_dir: Path = DATA_SESSION_DIR,
    ) -> None:
        self.ttl = ttl
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _path(self, session_id: str) -> Path:
        return self.storage_dir / f"{session_id}.json"

    def _to_dict(self, s: Session) -> dict:
        return {
            "session_id":       s.session_id,
            "account":          s.account,
            "conversation_url": s.conversation_url,
            "created_at":       s.created_at,
            "last_used":        s.last_used,
            "turn_count":       s.turn_count,
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
                json.dumps(self._to_dict(s), indent=2), encoding="utf-8"
            )
        except Exception as exc:
            log.warning("SessionStore: failed to save %s: %s", s.session_id[:8], exc)

    def _delete_from_disk(self, session_id: str) -> None:
        try:
            self._path(session_id).unlink(missing_ok=True)
        except Exception as exc:
            log.warning("SessionStore: failed to delete %s: %s", session_id[:8], exc)

    # ------------------------------------------------------------------ #
    # Startup restore
    # ------------------------------------------------------------------ #
    def load_from_disk(self) -> int:
        """
        Load all non-expired sessions from disk into memory on startup.
        Expired files are deleted immediately.
        Returns the number of sessions restored.
        """
        restored = 0
        removed  = 0
        for path in self.storage_dir.glob("*.json"):
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                s = self._from_dict(d)
                if s.is_expired(self.ttl):
                    path.unlink(missing_ok=True)
                    removed += 1
                    continue
                self._sessions[s.session_id] = s
                restored += 1
                log.debug(
                    "SessionStore: restored %s (account=%s, url=%s, turns=%d)",
                    s.session_id[:8], s.account,
                    (s.conversation_url or "")[:60], s.turn_count,
                )
            except Exception as exc:
                log.warning("SessionStore: failed to read %s: %s", path.name, exc)
        if restored:
            log.info("SessionStore: restored %d session(s) (%d expired removed)", restored, removed)
        else:
            log.debug("SessionStore: no active sessions on disk (%d expired removed)", removed)
        return restored

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def create(
        self,
        session_id: Optional[str] = None,
        account: Optional[str] = None,
    ) -> Session:
        sid = session_id or uuid.uuid4().hex
        s = Session(session_id=sid, account=account)
        self._sessions[sid] = s
        self._save_to_disk(s)
        return s

    def get(self, session_id: str) -> Optional[Session]:
        """Return the Session, or None if absent / expired (auto-deletes on expiry)."""
        s = self._sessions.get(session_id)
        if s is None:
            return None
        if s.is_expired(self.ttl):
            del self._sessions[session_id]
            self._delete_from_disk(session_id)
            log.info("SessionStore: session %s expired — deleted", session_id[:8])
            return None
        return s

    def get_or_create(
        self,
        session_id: Optional[str],
        account: Optional[str] = None,
    ) -> Session:
        if session_id:
            existing = self.get(session_id)
            if existing:
                return existing
        return self.create(session_id=session_id, account=account)

    def update(self, s: Session) -> None:
        """Persist an updated Session back to memory and disk."""
        self._sessions[s.session_id] = s
        self._save_to_disk(s)

    def bump_turn(self, session_id: str) -> None:
        s = self.get(session_id)
        if s:
            s.turn_count += 1
            s.touch()
            self.update(s)

    def all_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    def cleanup_expired(self) -> int:
        """Remove all expired sessions from memory and disk. Returns count removed."""
        expired = [
            sid for sid, s in self._sessions.items() if s.is_expired(self.ttl)
        ]
        for sid in expired:
            del self._sessions[sid]
            self._delete_from_disk(sid)
        if expired:
            log.debug("SessionStore: cleaned %d expired session(s)", len(expired))
        return len(expired)


# =========================================================================== #
# Enhanced LocalWorker
# =========================================================================== #
class LocalWorker:
    def __init__(
        self,
        vps_url: str,
        token: str,
        num_workers: int,
        headless: bool = True,
        session_ttl: int = _SESSION_TTL,
    ) -> None:
        self.vps_url = vps_url
        self.token = token
        self.num_workers = num_workers
        self.headless = headless
        self.worker_id: Optional[str] = None  # Assigned by VPS
        self.pool: Optional[BrowserPool] = None
        self._stop = asyncio.Event()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        # Session store with TTL; restore non-expired sessions from disk on start.
        self.session_store = SessionStore(ttl=session_ttl)
        self.session_store.load_from_disk()
        # Per-session asyncio.Lock — prevents two concurrent CONTINUE requests
        # to the same session from colliding.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Timestamps of when each lock was last used (for TTL-based GC).
        self._session_locks_meta: dict[str, float] = {}
        self._keepalive_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # Main event loop reference

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        # Save reference to main event loop for CLI thread
        self._loop = asyncio.get_running_loop()
        
        self.pool = BrowserPool(num_slots=self.num_workers, headless=self.headless)
        await self.pool.start()
        _s = self.pool.status_summary()
        log.info(
            "Worker pool ready: %d idle, %d busy, %d dead (total %d)",
            _s["idle"], _s["busy"], _s["dead"], _s["total"],
        )

        # Start interactive CLI in background thread
        threading.Thread(target=self._run_cli_loop, daemon=True).start()

        try:
            await self._connect_loop()
        finally:
            if self.pool:
                await self.pool.stop()

    async def _connect_loop(self) -> None:
        """Connect + auto-reconnect with backoff."""
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
            # Register (VPS will assign worker_id)
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "backend": "deepseek",   # ← model routing key (unified VPS)
                        "token": self.token,
                        "hostname": socket.gethostname(),
                        "max_concurrent": self.num_workers,
                        # VPS expects account-name strings; list_accounts() now
                        # returns dicts, so extract the names for wire compat.
                        "accounts": [a["account"] for a in self.pool.list_accounts()]
                        if self.pool else [],
                    }
                )
            )
            ack = json.loads(await ws.recv())
            if ack.get("type") != "registered":
                raise RuntimeError(f"Registration rejected: {ack}")
            self.worker_id = ack.get("worker_id")
            log.info("Registered with VPS as %s", self.worker_id)

            # Start keepalive ping
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            # Start background cleanup (expired sessions + idle locks)
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

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
            elif mtype == "ping":
                await ws.send(json.dumps({"type": "pong", "worker_id": self.worker_id}))

    async def _keepalive_loop(self) -> None:
        """Send periodic ping to VPS to prevent idle timeout."""
        try:
            while not self._stop.is_set():
                await asyncio.sleep(30)
                if self._ws:
                    await self._ws.send(
                        json.dumps({"type": "ping", "worker_id": self.worker_id})
                    )
        except Exception:
            pass

    async def _cleanup_loop(self) -> None:
        """Background task: remove expired sessions and idle locks every 60 s."""
        try:
            while not self._stop.is_set():
                await asyncio.sleep(60)
                cleaned = self.session_store.cleanup_expired()
                if cleaned:
                    log.debug("Cleanup: removed %d expired session(s)", cleaned)
                await self._cleanup_session_locks()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("Cleanup loop error: %s", exc)

    # ------------------------------------------------------------------ #
    # Per-session lock helpers
    # ------------------------------------------------------------------ #
    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        self._session_locks_meta[session_id] = time.time()
        return self._session_locks[session_id]

    async def _cleanup_session_locks(self, ttl: float = 3600.0) -> None:
        """Remove per-session locks that have been idle for `ttl` seconds."""
        now = time.time()
        stale = [
            sid for sid, ts in self._session_locks_meta.items()
            if now - ts > ttl and not self._session_locks[sid].locked()
        ]
        for sid in stale:
            del self._session_locks[sid]
            del self._session_locks_meta[sid]
        if stale:
            log.debug("Cleaned %d idle session lock(s)", len(stale))

    # ------------------------------------------------------------------ #
    # Task Handling
    # ------------------------------------------------------------------ #
    async def _handle_task(self, ws, msg: dict) -> None:
        task_id = msg.get("task_id")
        request = msg.get("request", {})
        t_received = time.monotonic()
        prompt_preview = (request.get("prompt") or "")[:60].replace("\n", " ")
        mode = request.get("mode", "new")
        session_id = request.get("session_id")

        log.info(
            "[%s] TASK RECEIVED | mode=%s session=%s prompt=%r",
            task_id,
            mode,
            (session_id or "")[:8] or "-",
            prompt_preview,
        )

        try:
            # Feature 11: Reject stream: true
            if request.get("stream"):
                await self._send_error(
                    ws,
                    task_id,
                    "Streaming is not supported by this worker",
                    status=400,
                )
                return

            # Feature 3: Per-session lock (anti-collision for CONTINUE)
            t_exec_start = time.monotonic()
            if session_id and mode == "continue":
                lock = await self._get_session_lock(session_id)
                async with lock:
                    t_lock_wait = time.monotonic() - t_exec_start
                    if t_lock_wait > 0.05:
                        log.debug(
                            "[%s] Waited %.2fs for session lock", task_id, t_lock_wait
                        )
                    result = await self._execute_task(request)
            else:
                result = await self._execute_task(request)

            t_exec_elapsed = time.monotonic() - t_exec_start
            t_total_elapsed = time.monotonic() - t_received

            # Feature 10: Error forwarding with HTTP status
            if not result.get("ok"):
                error_msg = result.get("error", "Unknown error")
                status = self._map_error_to_status(error_msg)
                log.warning(
                    "[%s] TASK FAILED | elapsed=%.2fs | error=%s",
                    task_id, t_total_elapsed, error_msg[:120],
                )
                await self._send_error(ws, task_id, error_msg, status=status)
                return

            # Feature 6: Persist / update session after a successful task.
            # Store account so CONTINUE can pin back to the same account.
            if session_id:
                conv_url = result.get("conversation_url")
                account  = result.get("account")
                s = self.session_store.get_or_create(session_id, account=account)
                if conv_url:
                    s.conversation_url = conv_url
                if account and not s.account:
                    s.account = account
                s.touch()
                self.session_store.update(s)
                self.session_store.bump_turn(session_id)
                log.debug(
                    "Session %s updated: account=%s url=%s turns=%d",
                    session_id[:8], s.account,
                    (s.conversation_url or "")[:60], s.turn_count,
                )

            response_len = len(result.get("text") or "")
            log.info(
                "[%s] TASK DONE | total=%.2fs scrape=%.2fs | account=%s mode=%s "
                "response_chars=%d",
                task_id,
                t_total_elapsed,
                t_exec_elapsed,
                result.get("account", "-"),
                mode,
                response_len,
            )

            await ws.send(
                json.dumps(
                    {
                        "type": "result",
                        "task_id": task_id,
                        "result": result,
                        "worker_id": self.worker_id,
                    }
                )
            )
        except Exception as exc:
            t_total_elapsed = time.monotonic() - t_received
            log.error(
                "[%s] TASK ERROR | elapsed=%.2fs | %s",
                task_id, t_total_elapsed, exc, exc_info=True,
            )
            await self._send_error(ws, task_id, str(exc), status=500)

    async def _execute_task(self, request: dict) -> dict:
        """Execute task with enhanced features."""
        if not self.pool:
            return {"ok": False, "error": "Pool not initialized"}

        # Extract request fields
        prompt      = request.get("prompt", "")
        mode        = request.get("mode", "new")
        session_id  = request.get("session_id")
        model_tab   = request.get("model_tab", "instant")
        deep_think  = request.get("deep_think", False)
        web_search  = request.get("web_search", False)
        tool_msgs   = request.get("tool_messages")       # list[dict] for Turn 2
        preferred_account = request.get("preferred_account")
        # JSON API mode / tool calling: forwarded from the VPS so the scraper
        # can build the [SYSTEM CONTEXT]/[USER REQUEST] wrapper.
        tools         = request.get("tools")
        max_tokens    = request.get("max_tokens")
        system_prompt = request.get("system_prompt")

        # Feature 12: Attachment support via base64
        attachments = None
        if request.get("attachments"):
            attachments = []
            for att in request["attachments"]:
                attachments.append(
                    {
                        "filename": att.get("filename", "file"),
                        "data":     att.get("data", ""),
                        "mime_type": att.get("mime_type", "application/octet-stream"),
                    }
                )

        # Feature 2 + 4 (upgraded): CONTINUE → load session, extract
        # conversation_url AND the pinned account so we land on the
        # same browser slot that handled Turn 1.
        conversation_url: Optional[str] = None
        _mode_fallback = False
        if mode == "continue" and session_id:
            existing = self.session_store.get(session_id)
            if existing:
                conversation_url = existing.conversation_url
                # Pin to the same account that opened the conversation.
                if existing.account and not preferred_account:
                    preferred_account = existing.account
                log.info(
                    "CONTINUE session=%s account=%s url=%s",
                    session_id[:8], preferred_account,
                    (conversation_url or "")[:80],
                )
            else:
                log.warning(
                    "CONTINUE requested but session %s not found/expired — "
                    "falling back to NEW",
                    session_id[:8],
                )
                mode = "new"
                # BUG FIX #4: Flag the fallback so the client knows the
                # requested mode was not honoured. This flag propagates
                # through the result → VPS x_meta → chatCLI, allowing the
                # client to reset its session state and re-create the
                # session on the next message.
                _mode_fallback = True

        # Feature 4: preferred_account routing with fallback
        if preferred_account:
            available = [a["account"] for a in self.pool.list_accounts()]
            if preferred_account not in available:
                log.warning(
                    "Preferred account %s not available, using fallback",
                    preferred_account,
                )
                preferred_account = None

        # Run task through pool
        try:
            t0 = time.monotonic()
            # Turn 2 path: tool results → scrape_with_tool_result()
            if mode == "continue" and tool_msgs:
                # The caller sends the next user message (if any) after tool results.
                messages = request.get("messages", [])
                last_tool_idx = max(
                    (i for i, m in enumerate(messages) if m.get("role") == "tool"),
                    default=-1,
                )
                msgs_after = messages[last_tool_idx + 1:] if last_tool_idx >= 0 else []
                next_user = next(
                    (m["content"] for m in msgs_after
                     if m.get("role") == "user" and m.get("content")),
                    None,
                )
                log.info(
                    "CONTINUE Turn 2: %d tool result(s), next_user=%s",
                    len(tool_msgs),
                    repr(next_user[:40]) if next_user else "None",
                )
                result = await self.pool.run_task_with_tool_result(
                    tool_messages=tool_msgs,
                    next_user_msg=next_user,
                    conversation_url=conversation_url,
                    preferred_account=preferred_account,
                )
            else:
                result = await self.pool.run_task(
                    prompt=prompt,
                    mode=mode,
                    model_tab=model_tab,
                    deep_think=deep_think,
                    web_search=web_search,
                    attachments=attachments,
                    preferred_account=preferred_account,
                    conversation_url=conversation_url,
                    tools=tools,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                )
            elapsed = time.monotonic() - t0

            # BUG FIX #4: Inject mode_fallback flag into the result so
            # the VPS can surface it in x_meta and the client knows when
            # mode="continue" was silently downgraded to "new".
            if _mode_fallback:
                result["mode_fallback"] = True

            log.debug(
                "_execute_task done | ok=%s elapsed=%.2fs mode=%s account=%s fallback=%s",
                result.get("ok"), elapsed, mode, result.get("account", "-"),
                _mode_fallback,
            )
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _send_error(
        self, ws, task_id: str, error: str, status: int = 500
    ) -> None:
        """Feature 13: Send error message to VPS (triggers reject_future)."""
        await ws.send(
            json.dumps(
                {
                    "type": "error",
                    "task_id": task_id,
                    "error": error,
                    "status": status,
                    "worker_id": self.worker_id,
                }
            )
        )

    def _map_error_to_status(self, error: str) -> int:
        """Feature 10: Map error messages to HTTP status codes."""
        error_lower = error.lower()
        if any(
            phrase in error_lower
            for phrase in ["rate limit", "too many requests", "请求过于频繁"]
        ):
            return 429
        if any(phrase in error_lower for phrase in ["timeout", "timed out"]):
            return 504
        if any(phrase in error_lower for phrase in ["not found", "404"]):
            return 404
        if any(
            phrase in error_lower
            for phrase in ["unauthorized", "authentication", "login"]
        ):
            return 401
        return 500

    # ------------------------------------------------------------------ #
    # Feature 7: Interactive CLI (REPL)
    # ------------------------------------------------------------------ #
    def _run_cli_loop(self) -> None:
        """Run interactive CLI in background thread."""
        print("\n" + "=" * 60)
        print("🎮 Interactive Worker Console")
        print("=" * 60)
        print("Commands:")
        print("  list accounts      - Show all accounts")
        print("  add account NAME   - Add account runtime (auto-login)")
        print("  status             - Show pool status")
        print("  showheadless ACC   - Toggle headless for account")
        print("  quit               - Graceful shutdown")
        print("=" * 60 + "\n")

        while not self._stop.is_set():
            try:
                cmd = input("worker> ").strip()
                if not cmd:
                    continue
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._handle_command(cmd), self._loop
                    )
            except (EOFError, KeyboardInterrupt):
                print("\nGraceful shutdown...")
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._shutdown(), self._loop
                    )
                break
            except Exception as e:
                print(f"Error: {e}")

    async def _handle_command(self, cmd: str) -> None:
        """Handle CLI commands."""
        parts = cmd.split()
        if not parts:
            return

        command = parts[0].lower()

        # list accounts
        if command == "list" and len(parts) == 2 and parts[1] == "accounts":
            if not self.pool:
                print("❌ Pool not initialized")
                return
            accounts = self.pool.list_accounts()
            print(f"\n📋 Accounts ({len(accounts)}):")
            for i, acc in enumerate(accounts, 1):
                vis = " [visible]" if acc.get("no_headless") else ""
                print(
                    f"  {i}. {acc['account']} "
                    f"(slot#{acc['slot_id']}, {acc['status']}){vis}"
                )
            print()

        # add account NAME
        elif command == "add" and len(parts) == 3 and parts[1] == "account":
            account_name = parts[2]
            await self._add_account_runtime(account_name)

        # status
        elif command == "status":
            if not self.pool:
                print("❌ Pool not initialized")
                return
            _s = self.pool.status_summary()
            print(
                f"\n📊 Pool Status: {_s['idle']} idle, {_s['busy']} busy, "
                f"{_s['dead']} dead (total {_s['total']})"
            )
            print(f"   Worker ID: {self.worker_id}")
            print(f"   Connected: {self._ws is not None}")
            print()

        # showheadless ACC
        elif command == "showheadless" and len(parts) == 2:
            account_name = parts[1]
            await self._toggle_headless(account_name)

        # quit
        elif command == "quit":
            await self._shutdown()

        else:
            print(f"❌ Unknown command: {cmd}")
            print("   Type 'help' to see available commands")

    # ------------------------------------------------------------------ #
    # Feature 8: Add account runtime (auto-login)
    # ------------------------------------------------------------------ #
    async def _add_account_runtime(self, account_name: str) -> None:
        """Add account to pool and auto-login."""
        if not self.pool:
            print("❌ Pool not initialized")
            return

        try:
            print(f"➕ Adding account: {account_name}")
            self.pool.add_account(account_name)

            # Auto-login by spawning a slot for this account
            # The pool will handle login on first use
            print(f"✅ Account {account_name} added")

            # Feature 6: Send update_accounts to VPS
            await self._update_accounts_to_vps()

        except Exception as e:
            print(f"❌ Failed to add account: {e}")

    # ------------------------------------------------------------------ #
    # Feature 9: Toggle headless per account
    # ------------------------------------------------------------------ #
    async def _toggle_headless(self, account_name: str) -> None:
        """Toggle headless mode for a specific account."""
        if not self.pool:
            print("❌ Pool not initialized")
            return

        # Find slot with this account
        slot = None
        for s in self.pool.slots:
            if s.account == account_name:
                slot = s
                break

        if not slot:
            print(f"❌ Account {account_name} not found in active slots")
            return

        try:
            # Respawn slot with toggled headless
            current_headless = slot.scraper.headless if slot.scraper else True
            new_headless = not current_headless

            print(
                f"🔄 Toggling headless for {account_name}: {current_headless} → {new_headless}"
            )

            # Mark slot as dead and respawn
            slot.mark_dead()
            await self.pool._respawn_slot(slot.index, headless=new_headless)

            print(f"✅ Browser for {account_name} now visible={not new_headless}")

        except Exception as e:
            print(f"❌ Failed to toggle headless: {e}")

    # ------------------------------------------------------------------ #
    # Feature 6: Update accounts to VPS
    # ------------------------------------------------------------------ #
    async def _update_accounts_to_vps(self) -> None:
        """Send updated account list to VPS."""
        if not self._ws or not self.pool:
            return

        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "update_accounts",
                        "worker_id": self.worker_id,
                        # VPS expects account-name strings (see register payload).
                        "accounts": [a["account"] for a in self.pool.list_accounts()],
                    }
                )
            )
            log.info("Sent account update to VPS")
        except Exception as e:
            log.warning("Failed to send account update: %s", e)

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #
    async def _shutdown(self) -> None:
        """Graceful shutdown."""
        print("\n🛑 Shutting down worker...")
        self._stop.set()
        for task in (self._keepalive_task, self._cleanup_task):
            if task:
                task.cancel()
        if self._ws:
            await self._ws.close()
        sys.exit(0)


# =========================================================================== #
# Main
# =========================================================================== #
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PAF-ModelDeepSeek enhanced local worker with interactive CLI"
    )
    p.add_argument(
        "--vps",
        required=True,
        help="VPS WebSocket URL (e.g., ws://VPS_IP:8000/ws/worker)",
    )
    p.add_argument(
        "--token", required=True, help="Shared secret token for authentication"
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of concurrent browser slots (default 1)",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browsers in headless mode (default True)",
    )
    p.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browsers with visible UI (overrides --headless)",
    )
    p.add_argument(
        "--session-ttl",
        type=int,
        default=_SESSION_TTL,
        help=f"Session TTL in seconds (default: {_SESSION_TTL})",
    )
    return p.parse_args(argv)


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    headless = not args.no_headless if args.no_headless else args.headless

    worker = LocalWorker(
        vps_url=args.vps,
        token=args.token,
        num_workers=args.workers,
        headless=headless,
        session_ttl=args.session_ttl,
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