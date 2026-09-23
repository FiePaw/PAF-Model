#!/usr/bin/env python3
"""
start_chatgpt_chrome.py — start a REAL Chrome with remote debugging enabled,
bound to the SAME persistent profile the ChatGPT worker uses.

This is the strongest workaround for the Cloudflare Turnstile challenge on
auth.openai.com / chatgpt.com: the browser is a real Chrome that started
ITSELF (no Playwright launch flags, no AutomationControlled hints, nothing
injected before pages load). The worker then ATTACHES to it over CDP and
only reads the DOM / clicks — the process-level automation fingerprints
Turnstile detects at browser-launch time simply don't exist.

Typical flow (one terminal for Chrome, one for the worker):

  # Terminal 1 — start real Chrome with the worker's profile:
  python start_chatgpt_chrome.py --account account1

  # Log into chatgpt.com in that window by hand ONCE
  # (solve any Cloudflare checkbox yourself — it almost never appears here,
  #  because this is a real Chrome, not an automation-launched browser).

  # Terminal 2 — run the worker attached to that Chrome:
  set CHATGPT_CDP_ATTACH=1        # (Windows; export on Linux/Mac)
  python public.py --backend chatgpt --vps ws://VPS_IP:PORT/ws/worker --token YOUR_TOKEN

The Chrome window stays open until you stop it:
  python start_chatgpt_chrome.py --account account1 --stop

Flags:
  --account NAME   Profile / account name (default: account1) →
                   profiles/chatgpt/<NAME>/ (same dir the worker uses).
  --port PORT      CDP debugging port (default: 9222).
  --headless       Start Chrome headless (NOT recommended for the first
                   login — solve challenges visually first).
  --channel NAME   Browser binary: "chrome" (real Google Chrome, default),
                   "msedge", "brave", or "chromium" (Playwright's bundled
                   one — weakest option, avoid for Cloudflare).
  --url URL        Page to open after start (default: ChatGPT base_url).
  --stop           Stop the Chrome started earlier for this account
                   (reads the PID file written on start).
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import CHATGPT_CONFIG, PROFILES_DIR
from scrapers.utils import get_logger

log = get_logger("paf_chatgpt.chrome")

CHROME_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-client-side-phishing-detection",
    "--disable-default-apps",
    "--disable-extensions-except=<KEEP>",
    "--disable-hang-monitor",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "--noerrdialogs",
]


def _pid_file(account: str, port: int) -> Path:
    return PROFILES_DIR / "chatgpt" / f"{account}.chrome-{port}.pid"


def _find_chrome_binary(channel: str) -> str | None:
    """Locate a real Chrome-family binary on this machine."""
    import shutil

    candidates = {
        "chrome": ["google-chrome", "google-chrome-stable", "chrome"],
        "msedge": ["microsoft-edge", "microsoft-edge-stable", "msedge"],
        "brave": ["brave-browser", "brave"],
        "chromium": ["chromium", "chromium-browser"],
    }.get(channel, [channel])
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    # Common Windows install locations (shutil.which misses .exe there when
    # not on PATH).
    if sys.platform == "win32":
        for exe in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ):
            if Path(exe).exists():
                return exe
    return None


def _wait_for_cdp(port: int, timeout: float = 30.0) -> bool:
    import urllib.request

    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def start_chrome(account: str, port: int, headless: bool, channel: str, url: str) -> int:
    profile_dir = PROFILES_DIR / "chatgpt" / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    binary = _find_chrome_binary(channel)
    if not binary:
        log.error(
            "Tidak menemukan binary browser untuk channel=%r di PATH. Install "
            "Google Chrome, atau pakai --channel chromium (bundled Playwright — "
            "lebih lemah terhadap Cloudflare).", channel,
        )
        return 1

    args = [
        binary,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        *CHROME_ARGS,
    ]
    if headless:
        args.append("--headless=new")
    args.append(url)

    log.info("Starting %s (channel=%s) with profile %s on CDP port %s",
             binary, channel, profile_dir, port)
    proc = subprocess.Popen(args)

    if not _wait_for_cdp(port):
        log.error("Chrome started (pid %s) tapi CDP port %s tidak merespons dalam 30s.", proc.pid, port)
        return 1

    pid_file = _pid_file(account, port)
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    log.info("✅ Chrome siap — CDP: http://127.0.0.1:%s (pid %s, pid file: %s)",
             port, proc.pid, pid_file)
    print("\nSelanjutnya:")
    print("  1. Log into chatgpt.com di jendela Chrome ini (sekali saja).")
    print("  2. Jalankan worker dengan CHATGPT_CDP_ATTACH=1, mis.:")
    print("       set CHATGPT_CDP_ATTACH=1   (Windows)")
    print("       python public.py --backend chatgpt --vps ws://VPS_IP:PORT/ws/worker --token YOUR_TOKEN")
    print(f"  3. Untuk menghentikan Chrome ini: python start_chatgpt_chrome.py --account {account} --stop\n")

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    return 0


def stop_chrome(account: str, port: int) -> int:
    pid_file = _pid_file(account, port)
    if not pid_file.exists():
        log.error("Tidak ada PID file %s — Chrome mungkin tidak di-start lewat script ini.", pid_file)
        return 1
    pid = int(pid_file.read_text().strip())
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        else:
            os.kill(pid, signal.SIGTERM)
        log.info("Chrome (pid %s) dihentikan.", pid)
    except Exception as exc:
        log.warning("Gagal menghentikan pid %s: %s (mungkin sudah mati)", pid, exc)
    finally:
        pid_file.unlink(missing_ok=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a real Chrome for CDP attach (ChatGPT backend).")
    parser.add_argument("--account", default="account1", help="Account/profile name (default: account1)")
    parser.add_argument("--port", type=int, default=9222, help="CDP debugging port (default: 9222)")
    parser.add_argument("--headless", action="store_true", help="Start Chrome headless (not recommended for first login)")
    parser.add_argument("--channel", default="chrome", help='Browser binary: "chrome" (default), "msedge", "brave", "chromium"')
    parser.add_argument("--url", default=CHATGPT_CONFIG["base_url"], help="Page to open after start")
    parser.add_argument("--stop", action="store_true", help="Stop the Chrome started earlier for this account")
    args = parser.parse_args()

    if args.stop:
        raise SystemExit(stop_chrome(args.account, args.port))
    raise SystemExit(start_chrome(args.account, args.port, args.headless, args.channel, args.url))


if __name__ == "__main__":
    main()
