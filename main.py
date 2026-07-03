#!/usr/bin/env python3
"""
main.py — standalone CLI for PAF-ModelDeepSeek.

Examples
--------
  # single prompt (Instant tab) — password login via CLI
  python main.py --prompt "Explain async/await in Python" \\
      --email you@example.com --password 'yourpass'

  # single prompt (credentials from .env or cookies/<account>.auth.json)
  python main.py --prompt "Explain async/await in Python"

  # first run with captcha: solve it once visibly, profile remembers it
  python main.py --prompt "hi" --no-headless

  # Expert tab + DeepThink + web search
  python main.py --prompt "Latest on fusion energy" \\
      --model-tab expert --deep-think --web-search

  # batch from a file, 3 concurrent browsers, save code blocks
  python main.py --prompts-file prompts.txt --concurrent 3 --save-code

  # continue the previous conversation (do not open a new chat)
  python main.py --prompt "and summarize that" --mode continue

  # visible browser for debugging / captcha / re-login
  python main.py --prompt "hi" --no-headless
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from browser_pool_deepseek import BrowserPool
from config import DEEPSEEK_CONFIG
from scrapers.deepseek_scraper import DeepSeekScraper
from scrapers.utils import get_logger, to_json_str

log = get_logger("paf_deepseek.main")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PAF-ModelDeepSeek standalone scraper for chat.deepseek.com",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt", help="A single prompt to send")
    src.add_argument(
        "--prompts-file",
        help="Path to a text file with one prompt per line (batch mode)",
    )

    p.add_argument(
        "--concurrent", type=int, default=1,
        help="Number of concurrent browsers for batch mode (default 1)",
    )
    p.add_argument(
        "--mode", choices=["new", "continue"], default="new",
        help="'new' opens a fresh chat; 'continue' keeps the previous session",
    )
    p.add_argument(
        "--model-tab", choices=["instant", "expert", "vision"],
        default=DEEPSEEK_CONFIG["default_model_tab"],
        help="Layer-1 model tab to select before sending",
    )
    p.add_argument("--deep-think", action="store_true", help="Enable DeepThink")
    p.add_argument("--web-search", action="store_true", help="Enable web Search")
    p.add_argument(
        "--attach", action="append", default=[],
        help="Path to a file to attach (repeatable). Image input needs Vision.",
    )
    p.add_argument(
        "--no-headless", action="store_true",
        help="Run the browser visibly (monitoring / captcha / re-login)",
    )
    p.add_argument(
        "--save-code", action="store_true",
        help="Save extracted code blocks separately to output/code/",
    )
    p.add_argument(
        "--account",
        help="Account name from cookies/auth.json to use (default: first)",
    )

    # Credentials override. Normally credentials come from cookies/auth.json
    # (keyed by --account) or DEEPSEEK_EMAIL / DEEPSEEK_PASSWORD env vars.
    p.add_argument("--email", help="DeepSeek account email/phone for login")
    p.add_argument("--password", help="DeepSeek account password for login")
    return p.parse_args(argv)


def _print_result(result: dict, save_code: bool, scraper: DeepSeekScraper) -> None:
    if not result.get("ok"):
        log.error("Request failed: %s", result.get("error"))
        return
    print("\n" + "=" * 70)
    print(result["text"])
    print("=" * 70 + "\n")

    scraper.save_to_json(result)
    if save_code and result.get("code_blocks"):
        scraper.save_code_files(result["code_blocks"])
        log.info("Saved %d code block(s)", len(result["code_blocks"]))


async def _run_single(args: argparse.Namespace) -> None:
    async with DeepSeekScraper(
        headless=not args.no_headless, account=args.account,
        email=args.email, password=args.password,
    ) as scraper:
        result = await scraper.scrape(
            args.prompt,
            mode=args.mode,
            attachments=args.attach or None,
            model_tab=args.model_tab,
            deep_think=args.deep_think,
            web_search=args.web_search,
        )
        _print_result(result, args.save_code, scraper)


async def _run_batch(args: argparse.Namespace) -> None:
    prompts = [
        line.strip()
        for line in Path(args.prompts_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not prompts:
        log.error("No prompts found in %s", args.prompts_file)
        return

    log.info("Batch: %d prompt(s) across %d browser(s)",
             len(prompts), args.concurrent)
    pool = BrowserPool(
        num_slots=args.concurrent, headless=not args.no_headless,
        email=args.email, password=args.password,
    )
    await pool.start()
    try:
        async def _one(prompt: str) -> dict:
            return await pool.run_task(
                prompt,
                mode=args.mode,
                attachments=args.attach or None,
                model_tab=args.model_tab,
                deep_think=args.deep_think,
                web_search=args.web_search,
            )

        results = await asyncio.gather(*(_one(p) for p in prompts))
        # Persist a combined batch result.
        from scrapers.utils import dump_json
        from config import OUTPUT_DIR
        from datetime import datetime
        ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        out = OUTPUT_DIR / f"batch_{ts}.json"
        dump_json(
            [{"prompt": p, "result": r} for p, r in zip(prompts, results)], out
        )
        ok = sum(1 for r in results if r.get("ok"))
        log.info("Batch complete: %d/%d ok -> %s", ok, len(results), out)
        if args.save_code:
            tmp = DeepSeekScraper()
            for r in results:
                if r.get("ok") and r.get("code_blocks"):
                    tmp.save_code_files(r["code_blocks"])
    finally:
        await pool.stop()


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        if args.prompt:
            await _run_single(args)
        else:
            await _run_batch(args)
        return 0
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.error("Fatal: %s", exc)
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
