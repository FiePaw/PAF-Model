"""Ad-hoc check for login_chatgpt.wait_for_manual_login() polling logic
(no real network needed -- uses HTML stubs served via page.route() at a
real https://chatgpt.com/ URL, since _is_logged_in() now also requires
being on that domain -- see base_chatgpt.py bug fix).

Covers TWO scenarios:
  1. Single tab, no popup -- basic timeout + eventual login detection.
  2. POPUP scenario (the exact bug reported live): a second Page object
     appears mid-wait (simulating the "Log in" button opening a popup),
     while the ORIGINAL tab keeps showing stale logged-out-ish content.
     Only the POPUP actually becomes logged in. wait_for_manual_login()
     must detect success via the popup (not report a false positive from
     the stale original tab) and must update scraper._page to point at the
     popup.

Run: python3 tests/_manual_login_wait_check.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from scrapers.chatgpt_scraper import ChatGPTScraper
from login_chatgpt import wait_for_manual_login

LOGGED_OUT_HTML = '<html><body><button>Log in</button></body></html>'
LOGGED_IN_HTML = (
    '<html><body><div class="avatar">A</div>'
    '<main><textarea id="prompt-textarea"></textarea></main>'
    '</body></html>'
)
# Simulates the ORIGINAL tab right after the user clicks "Log in": the
# button is gone (already clicked / hidden by the SPA) but the OLD app
# shell markup is still sitting in the DOM underneath -- exactly the
# false-positive trap from the reported bug.
STALE_BACKGROUND_HTML = (
    '<html><body><main><textarea id="prompt-textarea"></textarea></main></body></html>'
)


async def scenario_single_tab():
    scraper = ChatGPTScraper(headless=True, account="stub-test")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        scraper._context = context
        scraper._page = page

        current_html = {"body": LOGGED_OUT_HTML}

        async def _handle_route(route):
            await route.fulfill(body=current_html["body"], content_type="text/html")

        await page.route("https://chatgpt.com/**", _handle_route)

        # Case 1: never logs in -> should time out and return False quickly.
        current_html["body"] = LOGGED_OUT_HTML
        await page.goto("https://chatgpt.com/")
        ok = await wait_for_manual_login(scraper, timeout=1.0, poll_interval=0.2)
        assert ok is False, "Expected timeout (False) when never logged in"
        print("Case 1 (never logs in, short timeout) -> False   OK")

        # Case 2: logs in partway through the wait window.
        current_html["body"] = LOGGED_OUT_HTML
        await page.goto("https://chatgpt.com/")

        async def _simulate_manual_login():
            await asyncio.sleep(0.5)
            current_html["body"] = LOGGED_IN_HTML
            await page.reload()

        asyncio.create_task(_simulate_manual_login())
        ok2 = await wait_for_manual_login(scraper, timeout=5.0, poll_interval=0.2)
        assert ok2 is True, "Expected wait_for_manual_login to detect the login and return True"
        print("Case 2 (logs in after 0.5s, single tab) -> True   OK")

        await browser.close()


async def scenario_popup():
    """Reproduces the exact reported bug: a popup opens, the ORIGINAL tab
    keeps stale app-shell markup in the background (no "Log in" button,
    but old chat UI still present) -- this must NOT be mistaken for a
    successful login. Only once the POPUP itself becomes logged in should
    wait_for_manual_login() report success, and it must point
    scraper._page at that popup afterwards.
    """
    scraper = ChatGPTScraper(headless=True, account="stub-test")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        original_page = await context.new_page()
        scraper._context = context
        scraper._page = original_page

        original_html = {"body": LOGGED_OUT_HTML}
        popup_html = {"body": "<html><body>loading...</body></html>"}

        async def _handle_original(route):
            await route.fulfill(body=original_html["body"], content_type="text/html")

        async def _handle_popup(route):
            await route.fulfill(body=popup_html["body"], content_type="text/html")

        await original_page.route("https://chatgpt.com/**", _handle_original)
        await original_page.goto("https://chatgpt.com/")

        # Simulate: user clicks "Log in" -> original tab's button
        # disappears but stale app markup remains (the false-positive
        # trap) -- AND a popup opens showing the Cloudflare/auth flow,
        # which only becomes chatgpt.com + logged in after some delay.
        original_html["body"] = STALE_BACKGROUND_HTML

        popup_page = await context.new_page()
        await popup_page.route("https://chatgpt.com/**", _handle_popup)
        await popup_page.goto("about:blank")  # starts off-domain (auth provider stand-in)

        async def _simulate_popup_flow():
            await asyncio.sleep(0.4)
            # still on the auth domain (off-domain stand-in), not logged in yet
            await asyncio.sleep(0.4)
            popup_html["body"] = LOGGED_IN_HTML
            await popup_page.goto("https://chatgpt.com/")

        asyncio.create_task(_simulate_popup_flow())

        ok = await wait_for_manual_login(scraper, timeout=5.0, poll_interval=0.2)
        assert ok is True, "Expected wait_for_manual_login to detect login via the POPUP"
        assert scraper._page is popup_page, (
            "BUG: scraper._page should be updated to the POPUP that actually "
            "logged in, not left pointing at the stale original tab"
        )
        print("Popup scenario: detected login via popup (not the stale original tab) -> OK")
        print("scraper._page correctly updated to point at the popup -> OK")

        await browser.close()


async def main():
    await scenario_single_tab()
    await scenario_popup()
    print("\nAll checks for login_chatgpt.wait_for_manual_login() passed "
          "(including the popup false-positive regression).")


if __name__ == "__main__":
    asyncio.run(main())
