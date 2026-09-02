"""
Verification for the REGRESSION fix: wait_for_response() must NOT hang
indefinitely when _is_generating() is a persistent false-positive (reports
True forever even though the text has genuinely finished and stopped
changing). This reproduces the live-log pattern where text was stable at
len=1406 for 18+ polls (~9s) while "still streaming" kept firing non-stop.

Fix: _is_generating() is now an ADVISORY signal with a bounded grace period
(~5s of the text being completely unchanged) rather than a hard gate that
can block forever.

Run: python3 test_stuck_signal_regression.py
"""
from __future__ import annotations

import asyncio
import time

import scrapers.base_deepseek as bd
from scrapers.base_deepseek import BaseAIChatScraper


class _DummyScraper(BaseAIChatScraper):
    def _response_selectors(self) -> list[str]:
        return ["div.ds-markdown"]

    async def send_prompt(self, prompt: str, mode: str = "new", **kwargs) -> str:
        raise NotImplementedError

    async def is_rate_limited(self) -> bool:
        return False

    async def is_session_expired(self) -> bool:
        return False

    def _extra_send_kwargs(self) -> dict:
        return {}

    def _validate_response(self, raw: str) -> tuple[bool, str]:
        return True, raw


async def main() -> None:
    scraper = _DummyScraper.__new__(_DummyScraper)
    scraper.page = object()

    FINAL_TEXT = '{"status":"success","choices":[{"index":0,"message":{"role":"assistant","content":"done"}}]}'
    _t_start = time.monotonic()

    async def fake_get_last_response_text(_diagnostic: bool = False) -> str:
        # Text is COMPLETE and stable from the very first poll onward.
        return FINAL_TEXT

    async def always_generating_broken() -> bool:
        # Simulates the observed regression: the indicator NEVER clears,
        # even though the text has been done and unchanged the whole time.
        return True

    scraper._get_last_response_text = fake_get_last_response_text
    scraper._is_generating = always_generating_broken
    scraper._dump_dom_diagnostic = lambda: asyncio.sleep(0)

    result = await scraper.wait_for_response(
        timeout=60.0,
        stability_secs=2.0,
        stability_polls=2,
        poll_interval=0.5,
        pre_send_text="",  # NEW mode -> any non-empty text is "new" immediately
    )
    elapsed = time.monotonic() - _t_start
    print(f"result={result!r} elapsed={elapsed:.2f}s")

    assert result == FINAL_TEXT, f"FAIL: expected final text, got {result!r}"
    # Must self-heal via the grace period (~5s), NOT hang for 90s (stall
    # watchdog) or longer.
    assert elapsed < 15.0, (
        f"FAIL: took {elapsed:.2f}s to accept an already-complete, "
        f"never-changing response \u2014 the persistent-false-positive "
        f"_is_generating() signal is still blocking acceptance for too long."
    )
    print("\nALL CHECKS PASSED \u2014 a persistently-stuck _is_generating() signal "
          "no longer blocks an already-complete response indefinitely; it "
          "self-heals within the bounded grace period.")


if __name__ == "__main__":
    asyncio.run(main())
