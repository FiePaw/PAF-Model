"""Ad-hoc acceptance check for _is_logged_in() against HTML stubs (Grok
backend). REUSES the exact same false-positive regression coverage the
ChatGPT backend had (see base_grok.py docstring): _is_logged_in() requires
being on grok.com AND the app shell having actually rendered (not just a
"Sign in"/"Log in" button being absent). page.set_content() alone leaves
page.url as "about:blank", so these stubs are served via page.route()
interception at a real https://grok.com/ URL instead, to exercise the real
code path without any actual network access.

Not part of the permanent test suite (no network / real Grok dependency
needed) -- run manually:  python3 tests/_manual_login_detection_check.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from scrapers.grok_scraper import GrokScraper

LOGGED_OUT_HTML = """
<html><body>
  <button>Sign in</button>
  <button>Create account</button>
  <textarea placeholder="Ask Grok"></textarea>
</body></html>
"""

LOGGED_IN_HTML = """
<html><body>
  <div class="avatar-menu">A</div>
  <main>
    <textarea id="prompt-textarea" placeholder="Ask Grok"></textarea>
  </main>
</body></html>
"""

BLANK_LOADING_HTML = "<html><body></body></html>"


async def main():
    scraper = GrokScraper(headless=True, account="stub-test")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        scraper._page = page

        current_html = {"body": LOGGED_OUT_HTML}

        async def _handle_route(route):
            await route.fulfill(body=current_html["body"], content_type="text/html")

        await page.route("https://grok.com/**", _handle_route)

        current_html["body"] = LOGGED_OUT_HTML
        await page.goto("https://grok.com/")
        logged_in = await scraper._is_logged_in()
        assert logged_in is False, "Expected logged-out HTML to report NOT logged in"
        print("Logged-out stub -> _is_logged_in() == False  OK")

        current_html["body"] = LOGGED_IN_HTML
        await page.goto("https://grok.com/")
        logged_in = await scraper._is_logged_in()
        assert logged_in is True, "Expected logged-in HTML (no Sign in button, app shell rendered) to report logged in"
        print("Logged-in stub  -> _is_logged_in() == True   OK")

        # Regression: a blank/loading page (no Sign in button YET, but also
        # no app shell rendered yet) must NOT be reported as logged in.
        current_html["body"] = BLANK_LOADING_HTML
        await page.goto("https://grok.com/")
        logged_in = await scraper._is_logged_in()
        assert logged_in is False, (
            "BUG: blank/loading page falsely reported as logged in "
            "(the exact false-positive race guarded against by the ChatGPT-era fix)"
        )
        print("Blank/loading stub -> _is_logged_in() == False  OK (regression guard)")

        # Regression: not even on grok.com (e.g. mid-redirect to an SSO
        # provider domain) must NOT be reported as logged in, even though
        # neither the Sign-in button nor the app shell exist on that page.
        await page.goto("about:blank")
        logged_in = await scraper._is_logged_in()
        assert logged_in is False, "BUG: off-domain/blank page falsely reported as logged in"
        print("Off-domain (about:blank) -> _is_logged_in() == False  OK (regression guard)")

        await browser.close()

    print("\nAll acceptance checks for login detection (+ false-positive regression) passed.")


if __name__ == "__main__":
    asyncio.run(main())
