"""Ad-hoc check: playwright-stealth integration in base_chatgpt._apply_stealth
doesn't throw, and navigator.webdriver is masked afterwards.

Run: python3 tests/_manual_stealth_check.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from scrapers.chatgpt_scraper import ChatGPTScraper


async def main():
    scraper = ChatGPTScraper(headless=True, account="stub-test")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        await scraper._apply_stealth(context)
        page = await context.new_page()
        await page.goto("about:blank")

        webdriver = await page.evaluate("navigator.webdriver")
        plugins_len = await page.evaluate("navigator.plugins.length")
        has_chrome = await page.evaluate("!!window.chrome")
        print("navigator.webdriver:", webdriver)
        print("navigator.plugins.length:", plugins_len)
        print("window.chrome present:", has_chrome)

        assert webdriver in (False, None, "undefined") or webdriver is None or webdriver is False, webdriver
        print("PASS: stealth applied without error, navigator.webdriver masked")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
