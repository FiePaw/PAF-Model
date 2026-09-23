#!/usr/bin/env python3
"""
login_chatgpt.py — one-time MANUAL login helper for the ChatGPT backend.

Use this when automatic headless login gets stuck behind a Cloudflare
Turnstile challenge on auth.openai.com (see CHANGELOG — this is a known,
documented risk; Turnstile detects Playwright/CDP as an automation
controller regardless of stealth patches or `channel="chrome"`, and no
client-side workaround can guarantee bypassing it).

This script opens a VISIBLE (headless=False) browser bound to the exact
SAME persistent profile the worker/pool uses in production
(profiles/chatgpt/<account>/ — see config/chatgpt.py / base_chatgpt.py).
You log in by hand once — including solving the Turnstile checkbox
yourself if it appears, entering email/password, clicking through
"Continue with password", etc. — and this script polls in the background
until it detects a successful login (the same `_is_logged_in()` check used
everywhere else in this backend: the "Log in" button is gone from the DOM).

Once you're logged in, the persistent Chromium profile remembers the
session on disk. From then on, running the worker normally in HEADLESS
mode reuses that same profile and skips the login flow entirely —
`ChatGPTScraper.ensure_authenticated()` checks `_is_logged_in()` FIRST and
only falls back to the automated login() flow (and therefore only ever
hits the Cloudflare-challenged path) if that check fails:

    python login_chatgpt.py --account account1        # ONE-TIME, visible
    python public.py --backend chatgpt --headless ...  # every run after, headless

This script never closes the browser on its own — after it detects login
(or times out), it always pauses and asks you to confirm before closing.
Answer "n" (or just Ctrl+C / EOF) at that prompt to leave the browser
window open for as long as you like.

The account name only needs to match the name your worker/pool will use
later (see cookies/authchatgpt.json). It does NOT need real credentials to
be present there for this script to work — you can log in with whatever
credentials you type into the browser by hand. Credentials in
authchatgpt.json are only used as a fallback if the saved session
eventually expires and the automated login() flow gets triggered again.

Run one instance of this script PER account you need to warm up:

    python login_chatgpt.py --account account1
    python login_chatgpt.py --account account2
    ...

Flags:
    --account NAME     Profile / account name (default: account1). Maps to
                        profiles/chatgpt/<NAME>/.
    --url URL          Page to open first (default: CHATGPT_CONFIG['base_url']).
    --timeout SECONDS  How long to wait for you to finish logging in by hand
                        before giving up (default: 900s = 15 minutes).
    --channel NAME     Optional Playwright browser channel, e.g. "chrome" to
                        use a real installed Google Chrome instead of the
                        bundled Chromium (same CHATGPT_BROWSER_CHANNEL
                        contingency used by the headless worker).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from config import CHATGPT_CONFIG
from scrapers.chatgpt_scraper import ChatGPTScraper
from scrapers.utils import get_logger

log = get_logger("paf_chatgpt.manual_login")


async def _find_logged_in_page(scraper: ChatGPTScraper):
    """Check EVERY currently open page/tab in the browser context (not just
    scraper.page) and return the first one that is chatgpt.com + logged in.

    BUG FIX: clicking "Log in" can open a POPUP window — a separate
    Playwright Page object. Polling only `scraper.page` (the original tab)
    while a popup is open checks the WRONG tab: the original tab can still
    show stale ChatGPT app markup in the background (satisfying the
    "Log in absent + app shell present" heuristic) while the real
    Cloudflare/auth flow is happening on the popup the user is actually
    looking at — causing a false "login detected" long before the user
    finished anything. Checking every open page avoids needing to know in
    advance whether a popup will appear, and self-heals once the popup
    closes or navigates back to chatgpt.com (whichever page ends up
    logged in is picked up automatically on the next poll).

    Returns the matching Page, or None if none of the open pages currently
    qualify as "logged in on chatgpt.com".
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
    scraper: ChatGPTScraper, timeout: float, poll_interval: float = 2.0,
) -> bool:
    """Poll every open tab/popup until one of them is chatgpt.com + logged
    in, on 2 CONSECUTIVE polls (debounced), or `timeout` elapses.

    On success, `scraper._page` is updated to point at whichever page
    object actually ended up logged in (which may be a popup that was
    opened partway through, not the original tab) so everything after this
    (save_cookies(), printing the profile path, etc.) operates on the
    correct page.

    Debouncing guards against any residual one-poll race (e.g. a page
    transitioning between two states exactly when polled) by requiring the
    signal to hold steady across two polls before trusting it — the same
    "stability" pattern used by wait_for_response() elsewhere in this
    codebase.

    Kept as a standalone, testable function (no real browser needed to
    unit-test the polling logic itself — see tests/_manual_login_wait_check.py).
    """
    deadline = time.monotonic() + timeout
    last_notice = 0.0
    consecutive_true = 0
    last_page_count = 1
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
                f" ({page_count} tab/window terbuka — popup terdeteksi, semua dipantau)"
                if page_count > 1 else ""
            )
            print(
                f"⏳ Waiting for you to finish logging in manually... "
                f"({remaining}s left){popup_note} — solve any Cloudflare challenge, "
                f"enter your email/password, click through 'Continue with "
                f"password' if shown."
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
        description="One-time manual login helper for the PAF-Model ChatGPT backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--account", default="account1", help="Account/profile name (default: account1)")
    parser.add_argument("--url", default=None, help="Page to open first (default: ChatGPT base_url)")
    parser.add_argument("--timeout", type=float, default=900.0, help="Seconds to wait for manual login (default 900)")
    parser.add_argument("--channel", default=None, help='Playwright browser channel, e.g. "chrome"')
    args = parser.parse_args(argv)

    if args.channel:
        import os
        os.environ["CHATGPT_BROWSER_CHANNEL"] = args.channel

    url = args.url or CHATGPT_CONFIG["base_url"]

    print("=" * 70)
    print(f"ChatGPT manual login — account: {args.account!r}")
    print(f"Profile: profiles/chatgpt/{args.account}/")
    from scrapers.base_chatgpt import DRIVER_NAME
    _drv_note = ("  (patched Playwright — CDP automation leaks removed)"
                 if DRIVER_NAME == "patchright" else "")
    print(f"Browser driver: {DRIVER_NAME}{_drv_note}")
    print("A VISIBLE browser window will open now. Log in by hand:")
    print("  1. Click 'Log in' if shown (skip if already on the chat UI).")
    print("  2. Solve any Cloudflare 'Verify you are human' checkbox yourself.")
    print("  3. Enter your email -> Continue.")
    print("  4. If a 'Check your inbox' page appears, click 'Continue with password'.")
    print("  5. Enter your password -> Continue.")
    print("  6. Wait for the normal ChatGPT chat UI to load.")
    print("This script detects success automatically, then ASKS before closing —")
    print("it will never close the browser without your confirmation.")
    print("=" * 70 + "\n")

    scraper = ChatGPTScraper(headless=False, account=args.account)
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

        await asyncio.sleep(CHATGPT_CONFIG.get("timeouts", {}).get("between_actions", 600) / 1000)
        try:
            await scraper.save_cookies()
        except Exception:
            pass
        profile_dir = scraper._profile_dir_for(args.account)
        sentinel = profile_dir / "cookies_seeded"
        try:
            sentinel.write_text("1", encoding="utf-8")
        except Exception:
            pass

        print(f"\n✅ Login detected — account '{args.account}' is now authenticated.")
        print(f"   Session saved in profile: {profile_dir}")
        print("   You can now run the headless worker normally, e.g.:")
        print(f"   python public.py --backend chatgpt --vps ws://VPS_IP:PORT/ws/worker "
              f"--token YOUR_TOKEN")
        print("   It will reuse this profile and skip the login flow entirely.\n")

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
