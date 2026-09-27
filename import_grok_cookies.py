#!/usr/bin/env python3
"""
import_grok_cookies.py — seed a Grok account profile from cookies exported
manually out of a normal browser (Cookie-Editor extension). REPLACES
import_chatgpt_cookies.py entirely.

This is the ZERO-AUTOMATION login path — REUSED VERBATIM from the ChatGPT
backend's mitigation ladder (layer 5, see GROK_BACKEND.md §6) and the
exact same technique the owner-supplied reference scraper relies on
(login-capture-grok.js: capture cookies from a fully manual login, then
load them into every subsequent automated run — grok.js itself never
authenticates, it only ever loads pre-captured cookies): you log into
grok.com once in your everyday browser — no Playwright, no CDP, nothing
for Cloudflare/anti-bot checks to detect — export the cookies, and this
script injects them into the persistent profile the worker uses. No login
flow (and therefore no SSO/Cloudflare challenge) is ever executed here at
all, for either the automated worker OR this import step.

How to export the cookies:
  1. In your NORMAL browser (the one you use daily), log into grok.com via
     whichever SSO provider you use (Google / X / Apple / email-link).
  2. Install the "Cookie-Editor" browser extension.
  3. While on grok.com, open Cookie-Editor → Export → Export JSON (this
     includes httpOnly cookies — required; "Export Header string" will
     NOT work).
  4. Save the file as e.g. cookies/grok_account1.cookies.json.

Then run:
  python import_grok_cookies.py --account account1 \
      --cookies cookies/grok_account1.cookies.json

The script:
  1. Loads + converts the export (Cookie-Editor format → Playwright format,
     via the same cookie_editor_json_to_playwright() helper the ChatGPT/
     Qwen cookie-seeding paths already use — no changes needed there).
  2. Opens the persistent profile profiles/grok/<account>/ (the SAME
     profile the worker/pool uses later).
  3. Navigates to grok.com, injects the cookies, reloads.
  4. Verifies the session is actually valid (_page_is_logged_in()).
  5. On success writes the profile sentinel and asks before closing the
     browser (never closes without your confirmation).

After this, run the headless worker normally — it reuses the seeded
profile and skips straight to a valid session (there is no login flow to
skip past — see scrapers/grok_scraper.py):

  python public.py --backend grok --vps ws://VPS_IP:PORT/ws/worker --token YOUR_TOKEN

Notes:
  • The account name must exist in cookies/authgrok.json (email/password
    fields are optional/reserved and not required for this backend).
  • Export cookies while LOGGED IN on grok.com — exporting from an SSO
    provider's own domain or the logged-out homepage won't contain the
    session cookies.
  • If verification fails, the most common causes are: export taken while
    logged out, export without httpOnly cookies, or an expired session —
    re-export and try again.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from config import GROK_CONFIG
from login_grok import confirm_close
from scrapers.grok_scraper import GrokScraper
from scrapers.utils import get_logger, load_json

log = get_logger("paf_grok.cookie_import")


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
            "Export it via the Cookie-Editor extension on grok.com "
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
    """Human-readable hints about a failed import, derived from the export.

    The essential-cookie check targets "sso" (grok.com's httpOnly session
    cookie, confirmed from a real grok.com Cookie-Editor export — see
    GROK_BACKEND.md), analogous to ChatGPT's
    "__Secure-next-auth.session-token"."""
    hints = []
    domains = sorted({str(c.get("domain", "")).lstrip(".") for c in raw if isinstance(c, dict)})
    names = {str(c.get("name", "")) for c in raw if isinstance(c, dict)}
    if not any("grok.com" in d for d in domains):
        hints.append(
            "Tidak ada cookie untuk domain grok.com — export kemungkinan "
            f"diambil dari situs yang salah (domain di file: {domains}). "
            "Buka grok.com dulu (setelah login SSO), baru Export di Cookie-Editor."
        )
    if "sso" not in names:
        hints.append(
            "Cookie sesi 'sso' (httpOnly) tidak ditemukan — kemungkinan export "
            "dilakukan saat BELUM login, atau ekstensi menyembunyikan httpOnly "
            "cookies. Pastikan sudah login via SSO dan gunakan 'Export JSON'."
        )
    return hints


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a Grok worker profile from manually exported cookies.",
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
        os.environ["GROK_BROWSER_CHANNEL"] = args.channel

    try:
        cookies, raw = load_and_convert_cookies(args.cookies)
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 1

    print("=" * 70)
    print(f"Grok cookie import — account: {args.account!r}")
    print(f"Cookie file : {args.cookies} ({len(cookies)} cookie(s) setelah konversi)")
    print(f"Profile     : profiles/grok/{args.account}/")
    print("=" * 70 + "\n")

    scraper = GrokScraper(headless=args.headless, account=args.account)
    await scraper.launch_browser(account=args.account)
    page = scraper.page
    assert page is not None

    try:
        await page.goto(GROK_CONFIG["base_url"], wait_until="domcontentloaded", timeout=30_000)
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

            print(f"\n✅ Session valid — account '{args.account}' ter-seed dari cookies.")
            print(f"   Profile: {profile_dir}")
            print("   Jalankan worker headless seperti biasa; tidak ada login flow yang dieksekusi.\n")
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
