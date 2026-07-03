#!/usr/bin/env python3
"""
chat.py — Interactive chat client for PAF-ModelDeepSeek API.

Example usage dari API_USAGE.md — mengirim pesan ke /v1/chat/completions
dan mengelola sesi NEW / CONTINUE secara otomatis.

Usage
-----
  # Chat interaktif (sesi baru setiap kali dijalankan)
  python chat.py

  # Lanjutkan sesi yang sudah ada
  python chat.py --session-id math-session

  # Expert + DeepThink mode
  python chat.py --think-mode thinking

  # Base URL custom
  python chat.py --base-url http://192.168.1.10:9000

Commands (dalam loop chat)
--------------------------
  /new          Mulai sesi baru
  /status       Tampilkan session_id, conversation_url, account
  /think <mode> Ganti think_mode (instant/thinking/search/vision)
  /help         Tampilkan bantuan
  /quit /exit   Keluar
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from typing import Optional

import requests

# ─── ANSI colours ────────────────────────────────────────────────────────────
_R = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_RED = "\033[31m"

def _c(text: str, *codes: str) -> str:
    return "".join(codes) + str(text) + _R

def _bar(text: str = "") -> str:
    bar = "─" * 60
    if text:
        return f"\n{_c(bar, _DIM)}\n{_c(text, _BOLD, _CYAN)}\n{_c(bar, _DIM)}"
    return _c(bar, _DIM)


# ─── API client ──────────────────────────────────────────────────────────────
class DeepSeekClient:
    """
    Thin wrapper di atas /v1/chat/completions.
    Mengelola session_id dan pergantian mode new → continue secara otomatis.
    """

    def __init__(self, base_url: str, think_mode: str = "instant", timeout: int = 300) -> None:
        self.base_url  = base_url.rstrip("/")
        self.think_mode = think_mode
        self.timeout   = timeout

        # State sesi aktif
        self.session_id:       Optional[str] = None
        self.conversation_url: Optional[str] = None
        self.account_name:     Optional[str] = None
        self.turn_count:       int            = 0   # 0 = belum ada turn

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _mode(self) -> str:
        """Turn 1 = new, turn 2+ = continue."""
        return "new" if self.turn_count == 0 else "continue"

    def new_session(self, session_id: Optional[str] = None) -> None:
        """Reset ke sesi baru."""
        self.session_id       = session_id or _make_session_id()
        self.conversation_url = None
        self.account_name     = None
        self.turn_count       = 0
        print(_c(f"\n  Sesi baru: {self.session_id}", _GREEN))

    # ── Health check ─────────────────────────────────────────────────────────
    def health(self) -> dict:
        r = requests.get(f"{self.base_url}/health", timeout=10)
        r.raise_for_status()
        return r.json()

    # ── Chat ─────────────────────────────────────────────────────────────────
    def send(self, message: str) -> Optional[str]:
        """
        Kirim pesan ke API. Otomatis gunakan mode="new" untuk turn pertama,
        mode="continue" untuk turn berikutnya — persis seperti contoh di API_USAGE.md.
        """
        payload: dict = {
            "model":    "deepseek-chat",
            "messages": [{"role": "user", "content": message}],
        }

        # Session persistence
        if self.session_id:
            payload["session_id"] = self.session_id
            payload["mode"]       = self._mode()

        # Think mode
        if self.think_mode and self.think_mode != "instant":
            payload["think_mode"] = self.think_mode

        mode_label = payload.get("mode", "—").upper()
        print(
            _c(f"  [{mode_label}] ", _DIM) +
            _c(f"({self.think_mode}) ", _YELLOW) +
            _c("Mengirim… ", _DIM),
            end="", flush=True,
        )

        try:
            r = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=self.timeout,
            )
        except requests.exceptions.ConnectionError:
            print(_c("GAGAL — tidak bisa terhubung ke API.", _RED))
            return None
        except requests.exceptions.Timeout:
            print(_c("TIMEOUT — server tidak merespons.", _RED))
            return None

        if not r.ok:
            print(_c(f"HTTP {r.status_code}", _RED))
            try:
                err = r.json().get("error", {})
                print(_c(f"  {err.get('message', r.text)}", _RED))
            except Exception:
                print(_c(f"  {r.text[:200]}", _RED))
            return None

        print(_c("OK", _GREEN))

        data = r.json()
        meta = data.get("x_meta", {})

        # Update state dari response
        self.turn_count       += 1
        self.conversation_url  = meta.get("conversation_url") or self.conversation_url
        self.account_name      = meta.get("account_name")     or self.account_name
        # Sinkronkan session_id jika server mengembalikan nilai berbeda
        if meta.get("session_id"):
            self.session_id = meta["session_id"]

        return data["choices"][0]["message"]["content"]


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _make_session_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


def _print_response(text: str) -> None:
    print()
    print(_c("DeepSeek", _BOLD, _CYAN) + _c(":", _DIM))
    for line in text.splitlines():
        print(f"  {line}")
    print()


def _print_status(client: DeepSeekClient) -> None:
    print(_bar("Status"))
    print(f"  Session ID  : {_c(client.session_id or '—', _BOLD)}")
    print(f"  Turn        : {_c(str(client.turn_count), _CYAN)}")
    print(f"  Mode berikut: {_c(client._mode(), _GREEN)}")
    print(f"  Think mode  : {_c(client.think_mode, _YELLOW)}")
    print(f"  Account     : {_c(client.account_name or '—', _DIM)}")
    print(f"  URL         : {_c((client.conversation_url or '—')[:80], _DIM)}")
    print()


def _print_help() -> None:
    print(_bar("Perintah"))
    cmds = [
        ("/new [id]",       "Mulai sesi baru (opsional: beri nama session_id)"),
        ("/status",         "Tampilkan info sesi aktif"),
        ("/think <mode>",   "Ganti think_mode: instant / thinking / search / vision"),
        ("/help",           "Tampilkan bantuan ini"),
        ("/quit  /exit",    "Keluar"),
    ]
    for cmd, desc in cmds:
        print(f"  {_c(cmd.ljust(20), _BOLD)}{desc}")
    print()


# ─── CLI ─────────────────────────────────────────────────────────────────────
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PAF-ModelDeepSeek interactive chat (API client)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--base-url", default="http://16.79.2.204:9000",
        help="Base URL API (default: http://16.79.2.204:9000)",
    )
    p.add_argument(
        "--session-id", default=None,
        help="Session ID untuk dilanjutkan (CONTINUE). Kosongkan untuk sesi baru.",
    )
    p.add_argument(
        "--think-mode",
        choices=["instant", "thinking", "expert", "search", "vision", "fast", "deep", "reasoning"],
        default="instant",
        help="Mode awal (default: instant)",
    )
    p.add_argument(
        "--timeout", type=int, default=300,
        help="Timeout HTTP dalam detik (default: 300)",
    )
    p.add_argument(
        "--no-health-check", action="store_true",
        help="Skip health check saat startup",
    )
    return p.parse_args(argv)


# ─── Main loop ───────────────────────────────────────────────────────────────
def main(argv: list[str] = sys.argv[1:]) -> int:
    args   = _parse_args(argv)
    client = DeepSeekClient(
        base_url=args.base_url,
        think_mode=args.think_mode,
        timeout=args.timeout,
    )

    print(_bar("PAF-ModelDeepSeek · Chat"))
    print(_c(f"  API  : {args.base_url}", _DIM))

    # ── Health check ─────────────────────────────────────────────────────────
    if not args.no_health_check:
        print(_c("  Memeriksa koneksi ke API… ", _DIM), end="", flush=True)
        try:
            h = client.health()
            workers = h.get("workers", {})
            total   = workers.get("total_workers", 0)
            busy    = workers.get("busy_slots", 0)
            print(_c(f"OK  ({total} worker, {busy} busy)", _GREEN))
        except Exception as exc:
            print(_c(f"GAGAL ({exc})", _RED))
            print(_c("  Lanjutkan quand meme? (y/n): ", _YELLOW), end="")
            if input().strip().lower() != "y":
                return 1

    # ── Inisialisasi sesi ────────────────────────────────────────────────────
    if args.session_id:
        # CONTINUE: gunakan session_id yang diberikan
        # Turn 0 → mode = "new" secara default.
        # Kita set turn_count=1 agar mode = "continue" langsung.
        client.session_id = args.session_id
        client.turn_count = 1
        print(_c(f"\n  Melanjutkan sesi: {client.session_id}", _GREEN))
        print(_c("  (mode: CONTINUE — pesan pertama akan dikirim sebagai continue)", _DIM))
    else:
        client.new_session()

    print(_c("  Ketik pesan, atau /help untuk daftar perintah.\n", _DIM))

    # ── Chat loop ────────────────────────────────────────────────────────────
    while True:
        # Prompt indicator: ● = ada sesi aktif, ○ = belum
        has_session = bool(client.session_id)
        indicator   = _c("●", _GREEN) if has_session else _c("○", _DIM)
        sid_short   = (client.session_id or "none")[:10]
        mode_badge  = _c(client._mode(), _CYAN if client._mode() == "continue" else _GREEN)

        try:
            raw = input(f"{indicator} {_c(sid_short, _DIM)} [{mode_badge}] {_c('>', _BOLD)} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        # ── Commands ──────────────────────────────────────────────────────────
        if raw.startswith("/"):
            parts = raw.split(None, 1)
            cmd   = parts[0].lower()
            arg   = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("/quit", "/exit"):
                break

            elif cmd == "/new":
                client.new_session(arg or None)

            elif cmd == "/status":
                _print_status(client)

            elif cmd == "/think":
                valid = ["instant", "thinking", "expert", "search", "vision", "fast", "deep", "reasoning"]
                if not arg or arg not in valid:
                    print(_c(f"  Valid: {', '.join(valid)}", _YELLOW))
                else:
                    client.think_mode = arg
                    print(_c(f"  Think mode → {arg}", _GREEN))

            elif cmd == "/help":
                _print_help()

            else:
                print(_c(f"  Perintah tidak dikenal: {cmd}. Ketik /help.", _YELLOW))

            continue

        # ── Kirim ke API ──────────────────────────────────────────────────────
        response = client.send(raw)
        if response:
            _print_response(response)

    print(_c("\n  Sampai jumpa!\n", _BOLD, _GREEN))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
