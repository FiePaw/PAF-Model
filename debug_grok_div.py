#!/usr/bin/env python3
"""
debug_grok_div.py — Bedah DOM grok.com untuk debug kegagalan ekstraksi response.

Masalah: output Grok sudah keluar di browser, tapi worker tidak mendapatkan
hasil scrape (wait_for_response() timeout / text kosong).

Script ini membuka grok.com dengan profile account yang sama dengan worker,
mengirim satu prompt, lalu membedah struktur DOM-nya:
  1. Selector mana yang match (config lama vs kandidat baru)
  2. Berapa node yang match per selector
  3. Preview teks per node (apakah teks response benar-benar tertangkap)
  4. Dump HTML response terakhir ke file untuk dianalisis manual

Usage:
  python debug_grok_div.py --account account1 --prompt "Halo, jawab singkat: 1+1?"
  python debug_grok_div.py --account account1 --prompt "..." --no-headless

Hasil disimpan di debug/grok_dom_dump_<ts>.html + laporan di stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import GROK_CONFIG  # noqa: E402

# Selector LAMA (dipakai worker sekarang) vs KANDIDAT BARU — dibanding
# berdampingan supaya kelihatan mana yang putus.
SELECTOR_SETS: dict[str, list[str]] = {
    "config_lama (worker)": (
        GROK_CONFIG["selectors"]["message_items"]
        + GROK_CONFIG["selectors"]["main_area"]
    ),
    "kandidat_data-testid": [
        '[data-testid="assistant-message"]',
        '[data-testid="user-message"]',
        '[data-testid="message"]',
        '[data-message-id]',
        '[data-role="assistant"]',
    ],
    "kandidat_class": [
        "div.message-bubble",
        ".message",
        ".chat-message",
        '[class*="message"]',
        '[class*="bubble"]',
        '[class*="response"]',
        '[class*="markdown"]',
        "article",
    ],
}

# JS: hitung node + preview teks + kelas lengkapnya, semua dalam satu evaluate
# (jauh lebih cepat daripada query_selector_all satu-satu lewat CDP).
PROBE_JS = """
(selector) => {
    let nodes;
    try { nodes = Array.from(document.querySelectorAll(selector)); }
    catch (e) { return { error: String(e) }; }
    return {
        count: nodes.length,
        nodes: nodes.slice(-8).map((n, i, arr) => {
            const idx = nodes.length - arr.length + i;
            const r = n.getBoundingClientRect();
            return {
                index: idx,
                tag: n.tagName.toLowerCase(),
                testid: n.getAttribute('data-testid'),
                role: n.getAttribute('role'),
                cls: (n.className && n.className.toString ? n.className.toString() : '').slice(0, 160),
                text_len: (n.innerText || '').length,
                preview: (n.innerText || '').slice(0, 120).replace(/\\n/g, ' | '),
                visible: r.width > 0 && r.height > 0,
            };
        }),
    };
}
"""

BODY_SNAPSHOT_JS = """
() => {
    // Cari elemen yang teksnya paling mirip 'jawaban AI panjang' — bantu
    // menemukan container response yang tidak match selector manapun.
    const candidates = Array.from(document.querySelectorAll('div, section, article'))
        .filter(n => {
            const t = n.innerText || '';
            return t.length > 80 && n.children.length > 0
                && !n.querySelector('[data-testid="user-message"]');
        })
        .slice(-40)
        .map(n => ({
            tag: n.tagName.toLowerCase(),
            testid: n.getAttribute('data-testid'),
            cls: (n.className && n.className.toString ? n.className.toString() : '').slice(0, 160),
            text_len: (n.innerText || '').length,
            preview: (n.innerText || '').slice(0, 100).replace(/\\n/g, ' | '),
        }));
    return candidates;
}
"""


async def probe_all(page, label: str) -> None:
    print(f"\n{'=' * 72}\nPROBE: {label}\n{'=' * 72}")
    for set_name, selectors in SELECTOR_SETS.items():
        print(f"\n--- {set_name} ---")
        for sel in selectors:
            try:
                res = await page.evaluate(PROBE_JS, sel)
            except Exception as exc:
                print(f"  [!] {sel!r}: evaluate error: {exc}")
                continue
            if res.get("error"):
                print(f"  [!] {sel!r}: {res['error']}")
                continue
            if res["count"] == 0:
                print(f"  [ ] {sel!r}: 0 node")
                continue
            print(f"  [✓] {sel!r}: {res['count']} node")
            for n in res["nodes"]:
                vis = "vis" if n["visible"] else "hid"
                print(f"      #{n['index']} <{n['tag']}> testid={n['testid']} "
                      f"role={n['role']} {vis} len={n['text_len']}")
                print(f"         cls: {n['cls']}")
                print(f"         txt: {n['preview']!r}")


async def main() -> None:
    p = argparse.ArgumentParser(description="Debug DOM grok.com (div response)")
    p.add_argument("--account", default=None, help="Nama account di authgrok.json")
    p.add_argument("--prompt", default="Jawab singkat: apa ibu kota Indonesia?")
    p.add_argument("--mode", choices=["new", "continue"], default="new")
    p.add_argument("--no-headless", action="store_true")
    p.add_argument("--skip-send", action="store_true",
                   help="Hanya probe halaman yang sudah terbuka (tanpa kirim prompt)")
    args = p.parse_args()

    from scrapers.grok_scraper import GrokScraper

    scraper = GrokScraper(headless=not args.no_headless, account=args.account)
    await scraper.launch_browser(account=scraper.account)
    page = scraper.page
    assert page is not None

    ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    dump_path = Path("debug") / f"grok_dom_dump_{ts}.html"
    dump_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        ok = await scraper.ensure_authenticated()
        if not ok:
            print("\n[!] Session tidak valid — jalankan login_grok.py dulu.")
            return

        # 1) PROBE SEBELUM KIRIM — halaman kosong/composer saja
        await probe_all(page, f"SEBELUM KIRIM (mode={args.mode})")

        if not args.skip_send:
            # 2) Kirim prompt, lalu probe berkala selama generation
            print(f"\n[>] Mengirim prompt: {args.prompt!r}")
            await scraper.send_prompt(args.prompt, mode=args.mode)

            for i in range(3):
                await probe_all(page, f"SESUDAH KIRIM — snapshot #{i + 1}")
                if i < 2:
                    await asyncio.sleep(2.0)

            # 3) Bandingkan hasil ekstraksi worker vs realita DOM
            raw = await scraper._extract_current_text()
            print(f"\n{'=' * 72}\nHASIL _extract_current_text() (worker): "
                  f"len={len(raw)}\n{'=' * 72}")
            print(raw[:500] if raw else "  <<KOSONG — inilah bug-nya>>")

        # 4) Dump HTML lengkap untuk analisis manual
        html = await page.content()
        dump_path.write_text(html, encoding="utf-8")
        print(f"\n[💾] HTML halaman di-dump ke: {dump_path}")
        print("    Buka file itu, cari teks jawaban Grok, lalu lihat div/class "
              "apa yang membungkusnya — bandingkan dengan hasil probe di atas.")

        # 5) Snapshot kandidat tersembunyi (elemen besar yang tidak match selector)
        hidden = await page.evaluate(BODY_SNAPSHOT_JS)
        print(f"\n--- Kandidat container besar (tidak bergantung selector) ---")
        for c in hidden[-15:]:
            print(f"  <{c['tag']}> testid={c['testid']} len={c['text_len']} "
                  f"cls={c['cls'][:80]}")
            print(f"      txt: {c['preview']!r}")

        print("\nSelesai. Browser dibiarkan terbuka 60 detik — Ctrl+C untuk berhenti.")
        await asyncio.sleep(60)
    finally:
        await scraper.close_browser()


if __name__ == "__main__":
    asyncio.run(main())
