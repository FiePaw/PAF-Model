import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from scrapers.chatgpt_scraper import ChatGPTScraper

# Simulate the exact failure mode reported by the user: "Continue with phone
# number" sits BEFORE the real primary "Continue" button in DOM order.
HTML = """
<html><body>
  <input type="email" name="email" />
  <button type="button" id="phone-btn" onclick="window.__clicked='phone'">Continue with phone number</button>
  <button type="submit" id="continue-btn" onclick="window.__clicked='continue'">Continue</button>
</body></html>
"""


async def main():
    scraper = ChatGPTScraper(headless=True, account="stub-test")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.set_content(HTML)

        ok = await scraper._click_continue_button(page, timeout_ms=2000)
        clicked = await page.evaluate("window.__clicked")
        print("click_continue_button returned:", ok)
        print("actually clicked element:", clicked)
        assert ok is True
        assert clicked == "continue", f"BUG STILL PRESENT: clicked {clicked!r} instead of 'continue'"
        print("PASS: correct primary 'Continue' button was clicked, not 'Continue with phone number'")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
