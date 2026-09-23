#!/usr/bin/env python3
"""
import_chatgpt_cookies.py — seed a ChatGPT account profile from cookies
exported manually out of a normal browser (Cookie-Editor extension).

This is the ZERO-AUTOMATION login path for when Cloudflare Turnstile blocks
BOTH the automated headless login AND the visible manual-login helper
(login_chatgpt.py): you log into chatgpt.com once in your everyday browser —
no Playwright, no CDP, nothing for Cloudflare to detect — export the
cookies, and this script injects them into the persistent profile the
worker uses. The login flow (and therefore the Turnstile challenge on
auth.openai.com) is never executed at all.

How to export the cookies:
  1. In your NORMAL browser (the one you use daily), log into chatgpt.com.
  2. Install the "Cookie-Editor" browser extension.
  3. While on chatgpt.com, open Cookie-Editor → Export → Export JSON
     (this includes httpOnly cookies — required; "Export Header string"
     will NOT work).
  4. Save the file as e.g. cookies/chatgpt_account1.cookies.json.

Then run:
  python import_chatgpt_cookies.py --account account1 \
      --cookies cookies/chatgpt_account1.cookies.json

The script:
  1. Loads + converts the export (Cookie-Editor format → Playwright format,
     via the same cookie_editor_json_to_playwright() helper the Qwen
     legacy cookie-seeding path uses).
  2. Opens the persistent profile profiles/chatgpt/<account>/ (the SAME
     profile the worker/pool uses later).
  3. Navigates to chatgpt.com, injects the cookies, reloads.
  4. Verifies the session is actually valid (_page_is_logged_in()).
  5. On success writes the profile sentinel and asks before closing the
     browser (never closes without your confirmation).

After this, run the headless worker normally — it reuses the seeded profile
and skips the login flow entirely:

  python public.py --backend chatgpt --vps ws://VPS_IP:PORT/ws/worker --token YOUR_TOKEN

Notes:
  • The account name must exist in cookies/authchatgpt.json (email/password
    can be left empty — credentials are only a fallback if the imported
    session ever expires).
  • Export cookies while LOGGED IN on chatgpt.com — exporting from
    auth.openai.com or the logged-out homepage won't contain the session
    token.
  • If verification fails, the most common causes are: export taken while
    logged out, export without httpOnly cookies, or an expired session —
    re-export and try again.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from config import CHATGPT_CONFIG
from login_chatgpt import confirm_close
from scrapers.chatgpt_scraper import ChatGPTScraper
from scrapers.utils import get_logger, load_json

log = get_logger("paf_chatgpt.cookie_import")


def load_and_convert_cookies(path: str | Path) -> tuple[list[dict], list[dict]]:
    """Load a Cookie-Editor JSON export and convert it to Playwright format.

    Returns (converted_cookies, raw_entries) — raw entries are kept for
    diagnostics. Raises ValueError with a clear, actionable message when
    the file is missing/empty/in the wrong shape.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Cookie file not found: {path}\n"
            "Export it via the Cookie-Editor extension on chatgpt.com "
            "(Export → Export JSON) while LOGGED IN."
        )
    try:
        raw = load_json(path)
    except Exception as exc:
        raise ValueError(
            f"{path} is not valid JSON. Use Cookie-Editor → Export → Export JSON "
            f"(NOT 'Export Header string', which produces a raw header line). "
            f"Original error: {exc}"
        ) from exc

    # Some exporter tools wrap the list: {"cookies": [...]}. Unwrap it.
    if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
        raw = raw["cookies"]

    if not isinstance(raw, list) or not raw:
        raise ValueError(
            f"{path} does not contain a non-empty JSON list of cookies. "
            "Use Cookie-Editor → Export → Export JSON (not 'Export Header string')."
        )

    from scrapers.utils import cookie_editor_json_to_playwright
    converted = cookie_editor_json_to_playwright(raw)
    if not converted:
        raise ValueError(
            f"No usable cookie entries found in {path}. Each entry needs at "
            "least a 'name' and a 'value' field."
        )
    return converted, raw


def _diagnose(raw: list[dict]) -> list[str]:
    """Human-readable hints about a failed import, derived from the export."""
    hints = []
    domains = sorted({str(c.get("domain", "")).lstrip(".") for c in raw if isinstance(c, dict)})
    names = {str(c.get("name", "")) for c in raw if isinstance(c, dict)}
    if not any("chatgpt" in d for d in domains):
        hints.append(
            "Tidak ada cookie untuk domain chatgpt.com — export kemungkinan "
            f"diambil dari situs yang salah (domain di file: {domains}). "
            "Buka chatgpt.com dulu, baru Export di Cookie-Editor."
        )
    if not any("session-token" in n for n in names):
        hints.append(
            "Cookie session token (__Secure-next-auth.session-token) tidak "
            "ditemukan — kemungkinan export dilakukan saat BELUM login, atau "
            "ekstensi menyembunyikan httpOnly cookies. Pastikan sudah login "
            "dan gunakan 'Export JSON'."
        )
    return hints


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a ChatGPT worker profile from manually exported cookies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--account", default="account1", help="Account/profile name (default: account1)")
    parser.add_argument("--cookies", required=True, help="Path to the Cookie-Editor JSON export")
    parser.add_argument("--headless", action="store_true",
                        help="Run headless (default: visible, so you can see the result)")
    parser.add_argument("--channel", default=None, help='Playwright browser channel, e.g. "chrome"')
    args = parser.parse_args(argv)

    if args.channel:
        import os
        os.environ["CHATGPT_BROWSER_CHANNEL"] = args.channel

    try:
        cookies, raw = load_and_convert_cookies(args.cookies)
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 1

    print("=" * 70)
    print(f"ChatGPT cookie import — account: {args.account!r}")
    print(f"Cookie file : {args.cookies} ({len(cookies)} cookie(s) setelah konversi)")
    print(f"Profile     : profiles/chatgpt/{args.account}/")
    print("=" * 70 + "\n")

    scraper = ChatGPTScraper(headless=args.headless, account=args.account)
    await scraper.launch_browser(account=args.account)
    page = scraper.page
    assert page is not None

    try:
        await page.goto(CHATGPT_CONFIG["base_url"], wait_until="domcontentloaded", timeout=30_000)
        await scraper._context.add_cookies(cookies)
        await page.reload(wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2.0)  # beri waktu SPA menentukan state login

        if await scraper._page_is_logged_in(page):
            profile_dir = scraper._profile_dir_for(args.account)
            sentinel = profile_dir / "cookies_seeded"
            try:
                sentinel.write_text("1", encoding="utf-8")
            except Exception:
                pass
            try:
                await scraper.save_cookies()
            except Exception:
                pass

            print(f"\n✅ Session valid — account '{args.account}' ter-seed dari cookies.")
            print(f"   Profile: {profile_dir}")
            print("   Jalankan worker headless seperti biasa; login flow tidak akan dieksekusi.\n")
            if await confirm_close("Import cookies selesai."):
                await scraper.close_browser()
            else:
                print("Browser dibiarkan terbuka. Tutup manual kapan pun Anda siap.")
            return 0

        print("\n❌ Session TIDAK valid setelah cookies di-inject.")
        for hint in _diagnose(raw):
            print(f"   • {hint}")
        print("   Perbaiki export lalu jalankan script ini lagi.\n")
        if await confirm_close("Verifikasi gagal.", default_yes=False):
            await scraper.close_browser()
        else:
            print("Browser dibiarkan terbuka supaya Anda bisa memeriksa kondisinya.")
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted by user — leaving the browser as-is (not force-closing).")
        return 130


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
