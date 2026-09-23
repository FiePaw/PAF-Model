"""Ad-hoc acceptance check for Tahap B: _is_logged_in() against HTML stubs.

BUG FIX regression coverage: _is_logged_in() now also requires being on
chatgpt.com AND the app shell having actually rendered (not just "Log in"
being absent -- see base_chatgpt.py docstring for the false-positive this
guards against). page.set_content() alone leaves page.url as "about:blank",
so these stubs are served via page.route() interception at a real
https://chatgpt.com/ URL instead, to exercise the real code path without
any actual network access.

Not part of the permanent test suite (no network / real ChatGPT dependency
needed) -- run manually:  python3 tests/_manual_login_detection_check.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from scrapers.chatgpt_scraper import ChatGPTScraper

LOGGED_OUT_HTML = """
<html><body>
  <button>Log in</button>
  <button>Sign up</button>
  <textarea placeholder="Ask ChatGPT"></textarea>
</body></html>
"""

LOGGED_IN_HTML = """
<html><body>
  <div class="avatar-menu">A</div>
  <main>
    <textarea id="prompt-textarea" placeholder="Ask ChatGPT"></textarea>
  </main>
</body></html>
"""

BLANK_LOADING_HTML = "<html><body></body></html>"


async def main():
    scraper = ChatGPTScraper(headless=True, account="stub-test")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        scraper._page = page

        current_html = {"body": LOGGED_OUT_HTML}

        async def _handle_route(route):
            await route.fulfill(body=current_html["body"], content_type="text/html")

        await page.route("https://chatgpt.com/**", _handle_route)

        current_html["body"] = LOGGED_OUT_HTML
        await page.goto("https://chatgpt.com/")
        logged_in = await scraper._is_logged_in()
        assert logged_in is False, "Expected logged-out HTML to report NOT logged in"
        print("Logged-out stub -> _is_logged_in() == False  OK")

        current_html["body"] = LOGGED_IN_HTML
        await page.goto("https://chatgpt.com/")
        logged_in = await scraper._is_logged_in()
        assert logged_in is True, "Expected logged-in HTML (no Log in button, app shell rendered) to report logged in"
        print("Logged-in stub  -> _is_logged_in() == True   OK")

        # Regression: a blank/loading page (no Log in button YET, but also
        # no app shell rendered yet) must NOT be reported as logged in.
        current_html["body"] = BLANK_LOADING_HTML
        await page.goto("https://chatgpt.com/")
        logged_in = await scraper._is_logged_in()
        assert logged_in is False, (
            "BUG: blank/loading page falsely reported as logged in "
            "(the exact false-positive race reported live)"
        )
        print("Blank/loading stub -> _is_logged_in() == False  OK (regression guard)")

        # Regression: not even on chatgpt.com (e.g. mid-redirect to an auth
        # provider domain) must NOT be reported as logged in, even though
        # neither the Log in button nor the app shell exist on that page.
        await page.goto("about:blank")
        logged_in = await scraper._is_logged_in()
        assert logged_in is False, "BUG: off-domain/blank page falsely reported as logged in"
        print("Off-domain (about:blank) -> _is_logged_in() == False  OK (regression guard)")

        await browser.close()

    print("\nAll acceptance checks for Tahap B (+ false-positive regression) passed.")


if __name__ == "__main__":
    asyncio.run(main())
