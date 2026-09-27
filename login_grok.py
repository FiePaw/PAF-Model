#!/usr/bin/env python3
"""
login_grok.py — one-time MANUAL login helper for the Grok backend.
REPLACES login_chatgpt.py entirely.

grok.com authenticates via SSO (Google / X (Twitter) / Apple / email-link)
— there is no stable credential form to automate the way ChatGPT's
"email → Continue → password" flow could be. This script therefore never
tries to fill anything: it opens a VISIBLE (headless=False) browser bound
to the exact SAME persistent profile the worker/pool uses in production
(profiles/grok/<account>/ — see config/grok.py / base_grok.py), you log in
by hand once — pick whichever SSO provider you use, solve any Cloudflare
checkbox yourself if it appears — and this script polls in the background
until it detects a successful login (the same `_is_logged_in()` check used
everywhere else in this backend: no "Sign in"/"Log in" button, app shell
rendered).

The CDP/Cloudflare mitigation ladder here is REUSED VERBATIM from the
ChatGPT backend's login_chatgpt.py (see GROK_BACKEND.md §6): a visible,
persistent-profile browser is itself the "layer 4" mitigation. If
Cloudflare still challenges this visible flow, escalate to
import_grok_cookies.py (layer 5) or start_grok_chrome.py + GROK_CDP_ATTACH
(layer 6 — the strongest).

Once you're logged in, the persistent Chromium profile remembers the
session on disk. From then on, running the worker normally in HEADLESS
mode reuses that same profile and skips straight to a valid session —
`GrokScraper.ensure_authenticated()` checks `_is_logged_in()` FIRST and,
unlike the ChatGPT backend, has NO automated login fallback at all if that
check fails (see scrapers/grok_scraper.py) — it fails loud instead:

    python login_grok.py --account account1           # ONE-TIME, visible
    python public.py --backend grok --headless ...     # every run after, headless

This script never closes the browser on its own — after it detects login
(or times out), it always pauses and asks you to confirm before closing.
Answer "n" (or just Ctrl+C / EOF) at that prompt to leave the browser
window open for as long as you like.

The account name only needs to match the name your worker/pool will use
later (see cookies/authgrok.json). It does NOT need real credentials to be
present there for this script to work — log in with whatever SSO account
you type/click into the browser by hand.

Run one instance of this script PER account you need to warm up:

    python login_grok.py --account account1
    python login_grok.py --account account2
    ...

Flags:
    --account NAME     Profile / account name (default: account1). Maps to
                        profiles/grok/<NAME>/.
    --url URL          Page to open first (default: GROK_CONFIG['base_url']).
    --timeout SECONDS  How long to wait for you to finish logging in by hand
                        before giving up (default: 900s = 15 minutes).
    --channel NAME     Optional Playwright browser channel, e.g. "chrome" to
                        use a real installed Google Chrome instead of the
                        bundled Chromium (same GROK_BROWSER_CHANNEL
                        contingency used by the headless worker).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from config import GROK_CONFIG
from scrapers.grok_scraper import GrokScraper
from scrapers.utils import get_logger

log = get_logger("paf_grok.manual_login")


async def _find_logged_in_page(scraper: GrokScraper):
    """Check EVERY currently open page/tab in the browser context (not just
    scraper.page) and return the first one that is grok.com + logged in.

    Needed because an SSO click can open a POPUP window — a separate
    Playwright Page object. Polling only `scraper.page` (the original tab)
    while a popup is open checks the WRONG tab: the original tab can still
    show stale app markup in the background while the real SSO/Cloudflare
    flow is happening on the popup the user is actually looking at.
    Checking every open page avoids needing to know in advance whether a
    popup will appear, and self-heals once the popup closes or navigates
    back to grok.com.

    Returns the matching Page, or None if none of the open pages currently
    qualify as "logged in on grok.com".
    """
    context = scraper._context
    if context is None:
        return None
    for page in list(context.pages):
        try:
            if await scraper._page_is_logged_in(page):
                return page
        except Exception:
            continue
    return None


async def wait_for_manual_login(
    scraper: GrokScraper, timeout: float, poll_interval: float = 2.0,
) -> bool:
    """Poll every open tab/popup until one of them is grok.com + logged in,
    on 2 CONSECUTIVE polls (debounced), or `timeout` elapses.

    On success, `scraper._page` is updated to point at whichever page
    object actually ended up logged in (which may be an SSO popup that
    was opened partway through, not the original tab).

    Debouncing guards against a residual one-poll race by requiring the
    signal to hold steady across two polls before trusting it — the same
    "stability" pattern used by wait_for_response() elsewhere.
    """
    deadline = time.monotonic() + timeout
    last_notice = 0.0
    consecutive_true = 0
    while time.monotonic() < deadline:
        found = await _find_logged_in_page(scraper)
        if found is not None:
            consecutive_true += 1
            if consecutive_true >= 2:
                scraper._page = found
                return True
        else:
            consecutive_true = 0
        now = time.monotonic()
        if now - last_notice >= 15:
            remaining = int(deadline - now)
            page_count = len(scraper._context.pages) if scraper._context else 1
            popup_note = (
                f" ({page_count} tab/window terbuka — popup SSO terdeteksi, semua dipantau)"
                if page_count > 1 else ""
            )
            print(
                f"⏳ Waiting for you to finish logging in manually... "
                f"({remaining}s left){popup_note} — pick your SSO provider "
                f"(Google/X/Apple/email), solve any Cloudflare challenge yourself."
            )
            last_notice = now
        await asyncio.sleep(poll_interval)
    return False


async def confirm_close(prompt_prefix: str, default_yes: bool = True) -> bool:
    """Ask the user (blocking stdin, off the event loop) whether to close the
    browser now. Returns True only on an explicit "yes" (or Enter, which
    defaults to "yes" — flip `default_yes=False` for exit paths where NOT
    closing should be the default, e.g. after a failure worth inspecting).

    Never decides on its own — the caller always waits for this to return
    before touching the browser. On EOF/Ctrl+C the browser is left open.
    """
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = await asyncio.to_thread(input, f"{prompt_prefix} Tutup browser sekarang? {suffix}: ")
    except (EOFError, KeyboardInterrupt):
        print("\n(Tidak ada jawaban — browser dibiarkan TERBUKA. Tutup manual jika sudah selesai.)")
        return False
    ans = ans.strip().lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes", "ya")


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="One-time manual login helper for the PAF-Model Grok backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--account", default="account1", help="Account/profile name (default: account1)")
    parser.add_argument("--url", default=None, help="Page to open first (default: Grok base_url)")
    parser.add_argument("--timeout", type=float, default=900.0, help="Seconds to wait for manual login (default 900)")
    parser.add_argument("--channel", default=None, help='Playwright browser channel, e.g. "chrome"')
    args = parser.parse_args(argv)

    if args.channel:
        import os
        os.environ["GROK_BROWSER_CHANNEL"] = args.channel

    url = args.url or GROK_CONFIG["base_url"]

    print("=" * 70)
    print(f"Grok manual login — account: {args.account!r}")
    print(f"Profile: profiles/grok/{args.account}/")
    from scrapers.base_grok import DRIVER_NAME
    _drv_note = ("  (patched Playwright — CDP automation leaks removed)"
                 if DRIVER_NAME == "patchright" else "")
    print(f"Browser driver: {DRIVER_NAME}{_drv_note}")
    print("A VISIBLE browser window will open now. Log in by hand:")
    print("  1. Click 'Sign in' / 'Log in' if shown (skip if already on the chat UI).")
    print("  2. Pick whichever SSO provider you use (Google / X / Apple / email-link).")
    print("  3. Solve any Cloudflare 'Verify you are human' checkbox yourself.")
    print("  4. Wait for the normal Grok chat UI to load.")
    print("This script detects success automatically, then ASKS before closing —")
    print("it will never close the browser without your confirmation.")
    print("=" * 70 + "\n")

    scraper = GrokScraper(headless=False, account=args.account)
    await scraper.launch_browser(account=args.account)
    assert scraper.page is not None

    try:
        await scraper.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        log.warning("Initial navigation to %s failed (%s) — continuing anyway; "
                    "the browser window is open, you can navigate manually.", url, exc)

    try:
        already = await _find_logged_in_page(scraper)
        if already is not None:
            scraper._page = already
            print(f"✅ Account '{args.account}' is ALREADY logged in — nothing to do.")
            if await confirm_close("Akun sudah login."):
                await scraper.close_browser()
            else:
                print("Browser dibiarkan terbuka. Jalankan script ini lagi kapan saja untuk memeriksa ulang.")
            return 0

        ok = await wait_for_manual_login(scraper, timeout=args.timeout)
        if not ok:
            log.error(
                "Timed out after %.0fs waiting for manual login. Re-run this script "
                "with --timeout to allow more time, or check the browser window for "
                "an error you may have missed.", args.timeout,
            )
            if await confirm_close("Login belum terdeteksi selesai (timeout).", default_yes=False):
                await scraper.close_browser()
            else:
                print("Browser dibiarkan terbuka supaya Anda bisa lanjut login manual atau memeriksa error nya.")
            return 1

        await asyncio.sleep(GROK_CONFIG.get("timeouts", {}).get("between_actions", 800) / 1000)
        profile_dir = scraper._profile_dir_for(args.account)
        sentinel = profile_dir / "cookies_seeded"
        try:
            sentinel.write_text("1", encoding="utf-8")
        except Exception:
            pass

        print(f"\n✅ Login detected — account '{args.account}' is now authenticated.")
        print(f"   Session saved in profile: {profile_dir}")
        print("   You can now run the headless worker normally, e.g.:")
        print(f"   python public.py --backend grok --vps ws://VPS_IP:PORT/ws/worker "
              f"--token YOUR_TOKEN")
        print("   It will reuse this profile and skip straight to a valid session.\n")

        if await confirm_close("Login terkonfirmasi."):
            await scraper.close_browser()
        else:
            print("Browser dibiarkan terbuka. Tutup manual (atau jalankan lagi) kapan pun Anda siap.")
        return 0
    except KeyboardInterrupt:
        log.warning("Interrupted by user — leaving the browser as-is (not force-closing).")
        return 130


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
