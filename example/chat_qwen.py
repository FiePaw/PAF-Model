#!/usr/bin/env python3
"""
chat_cli.py – Interactive CLI Chatbot for AIChatScraper API Server

Mengirim pesan ke server secara terus-menerus dalam satu sesi (mode continue)
sampai pengguna mengetik perintah keluar.

Usage
-----
  python chat_cli.py
  python chat_cli.py --host 127.0.0.1 --port 8000
  python chat_cli.py --stream          # aktifkan streaming output
  python chat_cli.py --system "Kamu adalah tutor Python yang sabar."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Iterator
from colorama import init

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import requests
except ImportError:
    print("[ERROR] Library 'requests' tidak ditemukan.")
    print("        Jalankan: pip install requests")
    sys.exit(1)

# ── ANSI color codes ──────────────────────────────────────────────────────────
init(autoreset=True)
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
GRAY   = "\033[90m"
WHITE  = "\033[97m"

# Nonaktifkan warna jika terminal tidak mendukung (misal: pipe / redirect)
if not sys.stdout.isatty():
    RESET = BOLD = DIM = CYAN = GREEN = YELLOW = RED = BLUE = GRAY = WHITE = ""

# ── Konstanta ─────────────────────────────────────────────────────────────────
BANNER = f"""{CYAN}{BOLD}
╔══════════════════════════════════════════════════════╗
║          AIChatScraper  –  CLI Chatbot               ║
║          Backend: Qwen AI (chat.qwen.ai)             ║
╚══════════════════════════════════════════════════════╝
{RESET}"""

HELP_TEXT = f"""{YELLOW}
Perintah khusus:
  /help       Tampilkan perintah ini
  /clear      Bersihkan riwayat percakapan (mulai sesi baru)
  /history    Tampilkan riwayat percakapan sesi ini
  /status     Cek status server
  /exit       Keluar dari chatbot  (atau tekan Ctrl+C)
{RESET}"""

EXIT_COMMANDS  = {"/exit", "/quit", "/keluar", "exit", "quit"}
CLEAR_COMMANDS = {"/clear", "/reset", "/baru"}


# ─── Server Client ─────────────────────────────────────────────────────────────

class QwenChatClient:
    def __init__(self, base_url: str, stream: bool = False, timeout: int = 300):
        self.base_url       = base_url.rstrip("/")
        self.stream         = stream
        self.timeout        = timeout
        self.messages: list[dict] = []
        self.session        = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        # Session tracking (continue mode)
        self.session_id: str | None = None
        self.cookie_file: str | None = None
        self.conversation_url: str | None = None

    # ── Koneksi ───────────────────────────────────────────────────────────────

    def check_server(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def server_info(self) -> dict:
        try:
            r = self.session.get(f"{self.base_url}/", timeout=5)
            return r.json()
        except Exception:
            return {}

    # ── Kirim pesan ───────────────────────────────────────────────────────────

    def send(self, user_input: str, system_prompt: str = "") -> str | None:
        """
        Tambahkan pesan user ke history lalu kirim seluruh history ke server.
        Mode 'continue' aktif secara otomatis karena history berisi pesan
        assistant dari giliran sebelumnya.
        """
        self.messages.append({"role": "user", "content": user_input})

        payload = {
            "model": "qwen",
            "stream": self.stream,
            "messages": self._build_messages(system_prompt),
        }
        # Attach session ID header jika sudah ada (mode continue)
        if self.session_id:
            self.session.headers["X-Session-ID"] = self.session_id

        try:
            if self.stream:
                reply = self._send_streaming(payload)
            else:
                reply = self._send_blocking(payload)
        except requests.exceptions.ConnectionError:
            self._pop_last_user()
            return None
        except requests.exceptions.Timeout:
            self._pop_last_user()
            raise TimeoutError

        if reply:
            self.messages.append({"role": "assistant", "content": reply})

        return reply

    def _save_session_headers(self, resp: "requests.Response") -> None:
        """Simpan X-Session-ID, X-Cookie-File, X-Conversation-URL dari response."""
        sid = resp.headers.get("X-Session-ID", "")
        if sid:
            self.session_id = sid
        cf = resp.headers.get("X-Cookie-File", "")
        if cf:
            self.cookie_file = cf
        cu = resp.headers.get("X-Conversation-URL", "")
        if cu:
            self.conversation_url = cu

    def _build_messages(self, system_prompt: str) -> list[dict]:
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.extend(self.messages)
        return msgs

    def _send_blocking(self, payload: dict) -> str:
        r = self.session.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        self._save_session_headers(r)
        return r.json()["choices"][0]["message"]["content"]

    def _send_streaming(self, payload: dict) -> str:
        """Kirim request streaming dan cetak chunk satu per satu ke stdout."""
        full_text = ""
        with self.session.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            stream=True,
            timeout=self.timeout,
        ) as resp:
            resp.raise_for_status()
            self._save_session_headers(resp)
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        print(delta, end="", flush=True)
                        full_text += delta
                except (json.JSONDecodeError, KeyError):
                    continue
        print()  # newline setelah streaming selesai
        return full_text

    # ── History ───────────────────────────────────────────────────────────────

    def clear_history(self) -> None:
        self.messages.clear()
        self.session_id = None
        self.cookie_file = None
        self.conversation_url = None
        if "X-Session-ID" in self.session.headers:
            del self.session.headers["X-Session-ID"]

    def _pop_last_user(self) -> None:
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()

    def print_history(self) -> None:
        if not self.messages:
            print(f"{GRAY}  (belum ada riwayat percakapan){RESET}")
            return
        print()
        for i, msg in enumerate(self.messages, 1):
            role  = msg["role"]
            label = f"{CYAN}[You]{RESET}" if role == "user" else f"{GREEN}[Qwen]{RESET}"
            text  = msg["content"]
            preview = text[:300] + ("..." if len(text) > 300 else "")
            print(f"  {i}. {label} {preview}")
        print()


# ─── UI Helpers ────────────────────────────────────────────────────────────────

def print_thinking() -> None:
    print(f"\n{GRAY}  ● Qwen sedang berpikir...{RESET}", end="\r", flush=True)

def clear_thinking() -> None:
    print(" " * 40, end="\r")  # hapus baris thinking

def format_response(text: str) -> str:
    lines = text.split("\n")
    formatted = []
    for line in lines:
        formatted.append(f"  {line}")
    return "\n".join(formatted)

def print_separator() -> None:
    width = min(os.get_terminal_size().columns if sys.stdout.isatty() else 60, 60)
    print(f"{GRAY}{'─' * width}{RESET}")

def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")

def prompt_input() -> str:
    try:
        return input(f"\n{CYAN}{BOLD}You{RESET} {GRAY}[{timestamp()}]{RESET} › ").strip()
    except EOFError:
        return "/exit"


# ─── Main Loop ─────────────────────────────────────────────────────────────────

def run_chat(args: argparse.Namespace) -> None:
    base_url = f"http://{args.host}:{args.port}"
    client   = QwenChatClient(base_url=base_url, stream=args.stream, timeout=args.timeout)

    # Tampilkan banner
    print(BANNER)

    # Cek koneksi server
    print(f"{GRAY}  Menghubungkan ke server {base_url}...{RESET}")
    if not client.check_server():
        print(f"\n{RED}{BOLD}  [ERROR] Server tidak dapat dijangkau di {base_url}{RESET}")
        print(f"{YELLOW}  Pastikan api_server.py sudah dijalankan terlebih dahulu:{RESET}")
        print(f"  python api_server.py --host {args.host} --port {args.port}\n")
        sys.exit(1)

    info = client.server_info()
    pool = info.get("pool", {})
    print(f"{GREEN}  ✓ Terhubung!{RESET}  "
          f"{GRAY}workers={pool.get('max_workers','?')}  "
          f"mode={'streaming' if args.stream else 'blocking'}{RESET}")

    # Tampilkan system prompt jika ada
    if args.system:
        print(f"\n{GRAY}  System prompt: \"{args.system[:80]}{'...' if len(args.system)>80 else ''}\"{RESET}")

    print(HELP_TEXT)
    print_separator()
    print(f"{GRAY}  Ketik pesan Anda dan tekan Enter. Gunakan /exit untuk keluar.{RESET}")
    print_separator()

    turn = 0

    while True:
        try:
            user_input = prompt_input()
        except KeyboardInterrupt:
            print(f"\n\n{YELLOW}  Dihentikan oleh pengguna.{RESET}")
            break

        # ── Kosong ────────────────────────────────────────────────────────────
        if not user_input:
            continue

        # ── Perintah khusus ───────────────────────────────────────────────────
        if user_input.lower() in EXIT_COMMANDS:
            print(f"\n{YELLOW}  Sampai jumpa! Sesi berakhir setelah {turn} giliran.{RESET}\n")
            break

        if user_input.lower() in CLEAR_COMMANDS:
            client.clear_history()
            turn = 0
            print(f"\n{YELLOW}  ✓ Riwayat percakapan dihapus. Sesi baru dimulai.{RESET}")
            print_separator()
            continue

        if user_input.lower() == "/history":
            client.print_history()
            continue

        if user_input.lower() == "/help":
            print(HELP_TEXT)
            continue

        if user_input.lower() == "/status":
            info = client.server_info()
            pool = info.get("pool", {})
            print(f"\n{GRAY}  Status server:{RESET}")
            print(f"  URL          : {base_url}")
            print(f"  Active       : {pool.get('active_sessions', '?')}/{pool.get('max_workers', '?')}")
            print(f"  Total req    : {pool.get('total_requests', '?')}")
            print(f"  Pesan history: {len(client.messages)}")
            print(f"\n{GRAY}  Sesi aktif:{RESET}")
            print(f"  Session ID   : {client.session_id or '(belum ada)'}")
            print(f"  Cookie file  : {client.cookie_file or '(belum ada)'}")
            print(f"  Conv URL     : {client.conversation_url or '(belum ada)'}")
            print()
            continue

        # ── Kirim ke server ───────────────────────────────────────────────────
        turn += 1

        if not args.stream:
            print_thinking()

        t_start = time.time()

        try:
            reply = client.send(user_input, system_prompt=args.system)
        except TimeoutError:
            clear_thinking()
            print(f"\n{RED}  [TIMEOUT] Server tidak merespons dalam {args.timeout}s.{RESET}")
            print(f"{GRAY}  Coba lagi atau periksa log server.{RESET}\n")
            continue
        except requests.exceptions.ConnectionError:
            clear_thinking()
            print(f"\n{RED}  [ERROR] Koneksi ke server terputus.{RESET}")
            print(f"{GRAY}  Pastikan api_server.py masih berjalan.{RESET}\n")
            continue
        except requests.exceptions.HTTPError as e:
            clear_thinking()
            print(f"\n{RED}  [HTTP ERROR] {e}{RESET}\n")
            continue
        except Exception as e:
            clear_thinking()
            print(f"\n{RED}  [ERROR] {e}{RESET}\n")
            continue

        elapsed = time.time() - t_start

        if not args.stream:
            clear_thinking()

        # ── Tampilkan respons (blocking mode) ─────────────────────────────────
        if reply:
            if not args.stream:
                print(f"\n{GREEN}{BOLD}Qwen{RESET} {GRAY}[{timestamp()}] ({elapsed:.1f}s){RESET}")
                print(format_response(reply))
            else:
                print(f"{GRAY}  ── selesai dalam {elapsed:.1f}s{RESET}")
            if turn == 1 and client.session_id:
                print(
                    f"{GRAY}  ● Sesi dimulai │ "
                    f"cookie: {client.cookie_file} │ "
                    f"session: {client.session_id[:12]}...{RESET}"
                )
            print()
        else:
            print(f"\n{RED}  [ERROR] Tidak ada respons dari server.{RESET}\n")


# ─── CLI Argument Parsing ──────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chat-cli",
        description="Interactive CLI Chatbot for AIChatScraper API Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host",    default="108.137.15.61", help="Host server (default: 127.0.0.1)")
    p.add_argument("--port",    default=9000, type=int, help="Port server (default: 8000)")
    p.add_argument("--stream",  action="store_true", help="Aktifkan streaming output")
    p.add_argument("--timeout", default=300, type=int, help="Timeout request dalam detik (default: 300)")
    p.add_argument(
        "--system",
        default="",
        metavar="PROMPT",
        help='System prompt untuk Qwen (misal: "Kamu adalah tutor Python.")',
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        run_chat(args)
    except KeyboardInterrupt:
        print(f"\n\n{YELLOW}  Dihentikan.{RESET}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()