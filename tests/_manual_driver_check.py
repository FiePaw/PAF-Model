"""Ad-hoc check for the Grok backend browser-driver selection
(base_grok._load_async_api / DRIVER_NAME) and that the whole stub flow
(login-state check + persistent context) works under the selected driver.

Run: python3 tests/_manual_driver_check.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.pop("GROK_BROWSER_DRIVER", None)  # exercise the auto default

import scrapers.base_grok as bg
from scrapers.grok_scraper import GrokScraper

LOGGED_IN_HTML = (
    '<html><body><div class="avatar">A</div>'
    '<main><textarea id="prompt-textarea"></textarea></main>'
    '</body></html>'
)


async def main():
    print("Selected driver:", bg.DRIVER_NAME)
    assert bg.DRIVER_NAME in ("patchright", "playwright"), bg.DRIVER_NAME
    if bg.DRIVER_NAME != "patchright":
        print("NOTE: patchright not installed here — running on vanilla playwright. "
              "Install patchright (pip install patchright + python -m patchright install "
              "chromium) for the CDP-leak-masked driver.")
    else:
        print("patchright driver active — CDP automation leaks are masked at the driver level.")

    # Whole stub flow under the selected driver: launch persistent context,
    # serve a logged-in stub at a real grok.com URL, verify login check.
    scraper = GrokScraper(headless=True, account="driver-check")
    await scraper.launch_browser(account="driver-check")
    try:
        page = scraper.page
        assert page is not None

        async def _handle_route(route):
            await route.fulfill(body=LOGGED_IN_HTML, content_type="text/html")

        await page.route("https://grok.com/**", _handle_route)
        await page.goto("https://grok.com/")
        logged_in = await scraper._page_is_logged_in(page)
        assert logged_in is True, "Expected logged-in stub to be detected under the selected driver"
        print("Stub login-state check under driver:", bg.DRIVER_NAME, "-> OK")
    finally:
        await scraper.close_browser()

    print("\nAll driver-selection checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
